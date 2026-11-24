"""Lusha toolkit for the LushaAgent subagent.

Lusha publishes no Python SDK: its documented route for client code is the
REST API (``https://api.lusha.com``) described by the OpenAPI specs at
``https://docs.lusha.com/openapi.json`` (v3, current) and
``https://docs.lusha.com/v2/openapi.json`` (v2, legacy). This toolkit is a thin
``httpx`` wrapper over the v3 API, so it needs no extra package. One credential
drives it: ``LUSHA_API_KEY``, sent as the ``api_key`` header.

Every v3 operation has a tool, following the API's own sections:

  • Search, enrich and search-and-enrich for contacts and companies, plus the
    asynchronous Data Waterfall job a contact enrichment can leave behind
  • Prospecting (filter-based search) and the filter-value discovery endpoints
  • Lookalikes, buying groups, signals, signal scores and ICP fit scores
  • Website visitors and recorded sales conversations
  • Tables: contacts and companies tables share one tool each, picked by ``kind``
  • Webhooks: signal and opt-out subscriptions, delivery logs, the account secret
  • Account usage, and ``lusha_api_request`` for anything else, including the
    legacy v2 endpoints (``/v2/person``, ``/v2/company``, ``/prospecting/...``)

Two things about Lusha shape every tool here. **Credits**: search, enrichment,
prospecting, lookalikes, signals, scores and table exports spend the account's
credits, so each tool's description says what it charges for and
``lusha_get_account_usage`` shows the balance and the price of every action.
**Search then enrich**: searches return previews (no emails or phone numbers)
with a ``canReveal`` list priced per field; enrichment reveals them.
"""

from __future__ import annotations

import functools
import json
import logging
import re
from collections.abc import Callable
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.lusha.com"
_TIMEOUT = 60.0
_DETAIL_LIMIT = 4000
_LIST_LIMIT = 12000
_MAX_BATCH = 100

_CREDS_MSG = (
    "[missing-credentials] LUSHA_API_KEY not set. Ask the user for a Lusha API key "
    'with magic_link_request_secret("LUSHA_API_KEY") (Lusha dashboard: Enrich, API, '
    "Manage API keys; the account's plan must include API access)."
)
_REFUSED_HINT = (
    " Lusha refused the API key: it was revoked, mistyped or belongs to another "
    'account. Ask the user for a fresh one with magic_link_request_secret("LUSHA_API_KEY").'
)
_CREDIT_HINT = (
    " The Lusha account is out of credits for this action; lusha_get_account_usage "
    "shows the balance. The user can add credits or upgrade in the Lusha dashboard."
)
_FORBIDDEN_HINT = (
    " Lusha refused the account: either it is inactive (the user contacts "
    "support@lusha.com) or its plan does not include v3 API access (the user asks "
    "their Lusha account manager to enable it)."
)
_RATE_HINT = (
    " Lusha's rate limit or daily quota is used up; wait before retrying "
    "(lusha_get_account_usage shows the per-minute, hourly and daily limits)."
)
_GDPR_HINT = " Lusha blocked this record for legal (GDPR) reasons; it cannot be retrieved."
_SERVER_HINT = " This is a problem on Lusha's side; retry in a little while."


# ── HTTP ────────────────────────────────────────────────────────────────────


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop unset values so Lusha applies its own defaults."""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def _csv(value: str) -> list[str]:
    """Split a comma-separated (or newline-separated) argument into a list."""
    return [p.strip() for p in str(value or "").replace("\n", ",").split(",") if p.strip()]


def _ints(value: str) -> list[int]:
    """Comma-separated integers; anything that is not one raises ValueError."""
    return [int(p) for p in _csv(value)]


def _ranges(value: str) -> list[dict[str, int]]:
    """'1-50; 201-500; 1000-' as Lusha range objects ({min, max})."""
    out: list[dict[str, int]] = []
    for part in str(value or "").split(";"):
        lo, _, hi = part.strip().partition("-")
        if not part.strip():
            continue
        rng = _clean(
            {"min": int(lo) if lo.strip() else None, "max": int(hi) if hi.strip() else None}
        )
        if rng:
            out.append(rng)
    return out


def _error_text(resp: httpx.Response) -> str:
    """Lusha's own words for a failed call, as short as they stay readable."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:400]
    if isinstance(data, dict):
        for key in ("message", "error", "errors", "detail", "code"):
            if data.get(key):
                val = data[key]
                return (val if isinstance(val, str) else json.dumps(val))[:400]
    return json.dumps(data)[:400]


def _quota_left(resp: httpx.Response) -> str:
    """The remaining-quota headers Lusha sends, e.g. 'minute 0/60, daily 812/1000 left'."""
    parts = []
    for window, limit, left in (
        ("minute", "x-rate-limit-minute", "x-minute-requests-left"),
        ("hourly", "x-rate-limit-hourly", "x-hourly-requests-left"),
        ("daily", "x-rate-limit-daily", "x-daily-requests-left"),
    ):
        if resp.headers.get(left) is not None:
            cap = resp.headers.get(limit)
            parts.append(f"{window} {resp.headers[left]}{'/' + cap if cap else ''}")
    return f" (requests left: {', '.join(parts)})" if parts else ""


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> tuple[Any, str | None]:
    """Call the Lusha API. Returns ``(json, None)`` or ``(None, error_text)``."""
    key = get_credential("LUSHA_API_KEY")
    if not key:
        return None, _CREDS_MSG
    method = method.upper()
    where = f"Lusha {method} {path}"
    try:
        resp = httpx.request(
            method,
            f"{_BASE_URL}{path}",
            headers={
                "api_key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            params=params or None,
            json=body if body is not None else None,
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        return None, (
            f"[tool-error] {where}: Lusha did not answer within {_TIMEOUT:.0f}s "
            f"({type(exc).__name__}). Retry once; for a batch or a search, send fewer "
            "records or a smaller page size."
        )
    except Exception as exc:  # transport, bad URL, unencodable header: all become text
        return None, (
            f"[tool-error] {where}: the request could not be sent ({type(exc).__name__}: "
            f"{exc}). Check the ids and values passed, then retry."
        )
    if resp.status_code < 400:
        if not resp.content:
            return {}, None
        try:
            return resp.json(), None
        except ValueError:
            return {"raw": resp.text[:_DETAIL_LIMIT]}, None
    detail = _error_text(resp)
    msg = f"[tool-error] {where}: HTTP {resp.status_code}: {detail}"
    if resp.status_code == 401:
        msg += _REFUSED_HINT
    elif resp.status_code == 402:
        msg += _CREDIT_HINT
    elif resp.status_code == 403:
        msg += _FORBIDDEN_HINT
    elif resp.status_code == 429:
        msg += _RATE_HINT + _quota_left(resp)
    elif resp.status_code == 451:
        msg += _GDPR_HINT
    elif resp.status_code >= 500:
        msg += _SERVER_HINT
    return None, msg


# Lusha ids reaching a URL path (table, column, job, subscription and
# conversation ids) are plain tokens such as "482910", "c_0ce4d3",
# "wf_3f2b…" or a UUID. Nothing else may reach a path: httpx resolves "..", so
# an id such as "../../api/account/secret/regenerate" would turn an ungated
# read into a different endpoint behind the approval gate.
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")


def _seg(label: str, value: str) -> tuple[str, str | None]:
    """Validate one id for use as a URL path segment. Returns ``(id, error)``."""
    ident = str(value or "").strip()
    if not ident:
        return "", f"[tool-error] {label} is required; look it up with a list or search tool first."
    if not _ID_RE.match(ident):
        return "", (
            f"[tool-error] {label} {ident[:60]!r} is not a Lusha id (letters, digits, '_', "
            "'-', '.' and ':' only). Pass the id field from a list or search result."
        )
    return ident, None


def _loads(label: str, raw: str) -> tuple[Any, str | None]:
    if not str(raw or "").strip():
        return None, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"[tool-error] {label} is not valid JSON: {exc}"


def _json_object(label: str, raw: str) -> tuple[dict[str, Any], str | None]:
    """An optional JSON object argument: ``{}`` when empty, an error when not an object."""
    value, err = _loads(label, raw)
    if err:
        return {}, err
    if value is None:
        return {}, None
    if not isinstance(value, dict):
        return {}, f"[tool-error] {label} must be a JSON object of field names to values."
    return value, None


def _merge(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge ``extra`` into ``base`` (``extra`` wins on scalars and lists)."""
    out = dict(base)
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def _records(
    label: str, raw: str, single: dict[str, Any], *, cap: int = _MAX_BATCH
) -> tuple[list[dict[str, Any]], str | None]:
    """The records to send: a JSON array, or one record built from the single-record args."""
    parsed, err = _loads(label, raw)
    if err:
        return [], err
    one = _clean(single)
    if parsed is None:
        if not one:
            return [], f"[tool-error] Give {label} (a JSON array) or the fields of one record."
        return [one], None
    if isinstance(parsed, dict):
        parsed = [parsed]
    if not isinstance(parsed, list) or not parsed:
        return [], f"[tool-error] {label} must be a non-empty JSON array of objects."
    if not all(isinstance(r, dict) for r in parsed):
        return [], f"[tool-error] every entry of {label} must be a JSON object."
    if one:
        parsed = [*parsed, one]
    if len(parsed) > cap:
        return [], f"[tool-error] Lusha takes at most {cap} records per call; split the list."
    return parsed, None


def _ids(label: str, value: str, *, cap: int = _MAX_BATCH) -> tuple[list[str], str | None]:
    items = _csv(value)
    if not items:
        return [], f"[tool-error] Give at least one id in {label}."
    if len(items) > cap:
        return (
            [],
            f"[tool-error] Lusha takes at most {cap} ids per call in {label}; split the list.",
        )
    return items, None


def _kind(value: str) -> tuple[str, str | None]:
    """'contacts' or 'companies' (singular and capitalised spellings accepted)."""
    k = str(value or "").strip().lower()
    k = {"contact": "contacts", "company": "companies", "people": "contacts"}.get(k, k)
    if k not in ("contacts", "companies"):
        return "", "[tool-error] kind must be 'contacts' or 'companies'."
    return k, None


def _owner(email: str) -> tuple[dict[str, str], str | None]:
    addr = str(email or "").strip()
    if "@" not in addr:
        return {}, (
            "[tool-error] owner_email is required: the email of a user on the Lusha account "
            "the API key belongs to (Lusha acts on tables as that user). Ask the user for it."
        )
    return {"email": addr}, None


# ── Formatting ──────────────────────────────────────────────────────────────


def _join(*parts: Any, sep: str = " | ") -> str:
    return sep.join(str(p) for p in parts if p not in (None, "", [], {}))


def _clip(text: str, limit: int = _LIST_LIMIT) -> str:
    if len(text) > limit:
        return text[:limit] + "\n... (truncated; ask for fewer records or a smaller page)"
    return text


def _dump(obj: Any, label: str, limit: int = _DETAIL_LIMIT) -> str:
    """Non-empty fields of a record, as indented JSON clipped to a sane size."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if v not in (None, "", [], {})}
    text = json.dumps(obj, indent=2, default=str)
    if len(text) > limit:
        text = text[:limit] + "\n... (truncated)"
    return f"{label}:\n{text}"


def _compact(obj: Any, limit: int = 300) -> str:
    text = json.dumps(obj, default=str, separators=(",", ":"))
    return text if len(text) <= limit else text[:limit] + "..."


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _location(loc: Any) -> str:
    loc = _as_dict(loc)
    return _join(loc.get("city"), loc.get("state"), loc.get("country"), sep=", ")


def _billing(data: Any) -> str:
    """'Credits charged: 3.' plus Lusha's low-balance warning."""
    bill = _as_dict(_as_dict(data).get("billing"))
    parts = []
    if bill.get("creditsCharged") is not None:
        parts.append(f"Credits charged: {bill['creditsCharged']}.")
    if bill.get("lowBalance"):
        parts.append("Lusha reports the credit balance is running low (lusha_get_account_usage).")
    return " ".join(parts)


def _with_billing(text: str, data: Any) -> str:
    bill = _billing(data)
    return f"{text}\n{bill}" if bill else text


def _reveal_list(item: dict[str, Any]) -> str:
    """'can reveal: emails (1 cr), phones (5 cr)' from a preview's canReveal."""
    fields = []
    for r in _as_list(item.get("canReveal")):
        if isinstance(r, dict) and r.get("field"):
            cost = f" ({r['credits']} cr)" if r.get("credits") is not None else ""
            fields.append(f"{r['field']}{cost}")
        elif isinstance(r, str):
            fields.append(r)
    return f"can reveal: {', '.join(fields)}" if fields else ""


def _item_error(item: dict[str, Any]) -> str:
    err = item.get("error")
    if not err:
        return ""
    if isinstance(err, dict):
        return f"error: {_join(err.get('code'), err.get('message'), sep=' ')}"
    return f"error: {err}"


def _contact_name(c: dict[str, Any]) -> str:
    return str(c.get("fullName") or _join(c.get("firstName"), c.get("lastName"), sep=" ") or "?")


def _contact_role(c: dict[str, Any]) -> str:
    title = (
        _as_dict(c.get("jobTitle")).get("title")
        if isinstance(c.get("jobTitle"), dict)
        else c.get("jobTitle")
    )
    company = _as_dict(c.get("company"))
    org = _join(company.get("name"), company.get("domain"), sep=" ")
    return f"{title} @ {org}" if title and org else str(title or org or "")


def _contact_line(c: dict[str, Any]) -> str:
    seniority = _as_dict(c.get("jobTitle")).get("seniority")
    linkedin = _as_dict(c.get("socialLinks")).get("linkedin")
    roles = c.get("roles")
    return _join(
        _contact_name(c),
        _contact_role(c),
        seniority,
        _location(c.get("location")),
        linkedin,
        f"roles: {', '.join(map(str, roles))}" if isinstance(roles, list) and roles else "",
        f"score={c['score']}" if c.get("score") is not None else "",
        f"has: {', '.join(map(str, c['has']))}"
        if isinstance(c.get("has"), list) and c["has"]
        else "",
        _reveal_list(c),
        f"signals: {', '.join(map(str, c['signalTypes']))}" if c.get("signalTypes") else "",
        f"ref={c['clientReferenceId']}" if c.get("clientReferenceId") else "",
        _item_error(c),
        f"id={c.get('id', '?')}",
    )


def _company_line(o: dict[str, Any]) -> str:
    size = _as_dict(o.get("employeeCount"))
    if size.get("exact"):
        headcount = f"{size['exact']} employees"
    elif size.get("min") or size.get("max"):
        headcount = f"{size.get('min', '?')}-{size.get('max', '?')} employees"
    else:
        headcount = ""
    return _join(
        o.get("name") or o.get("companyName") or "(no name)",
        o.get("domain"),
        o.get("industry"),
        headcount,
        _location(o.get("location")),
        f"has: {', '.join(map(str, o['has']))}"
        if isinstance(o.get("has"), list) and o["has"]
        else "",
        _reveal_list(o),
        f"signals: {', '.join(map(str, o['signalTypes']))}" if o.get("signalTypes") else "",
        f"ref={o['clientReferenceId']}" if o.get("clientReferenceId") else "",
        _item_error(o),
        f"id={o.get('id', '?')}",
    )


def _rows(items: Any, line: Callable[[dict[str, Any]], str], heading: str, data: Any = None) -> str:
    items = [i for i in _as_list(items) if isinstance(i, dict)]
    pg = _as_dict(_as_dict(data).get("pagination"))
    meta = []
    if pg.get("page") is not None:
        meta.append(f"page {pg['page']}")
    if pg.get("total") is not None:
        meta.append(
            f"{pg['total']} total{'' if pg.get('totalGuaranteed', True) else ' (estimate)'}"
        )
    head = f"{heading}{' (' + ', '.join(meta) + ')' if meta else ''}"
    if not items:
        return _with_billing(f"{head}: none found.", data)
    out = [f"{head}:"] + [f"  {line(i)}" for i in items]
    return _with_billing(_clip("\n".join(out)), data)


def _phone_text(p: dict[str, Any]) -> str:
    flags = [p.get("type"), "do-not-call" if p.get("doNotCall") else "", p.get("dataSource")]
    return _join(p.get("number"), _join(*flags, sep=", "), sep=" ")


def _email_text(e: dict[str, Any]) -> str:
    return _join(
        e.get("email"),
        _join(e.get("type"), e.get("confidence"), e.get("dataSource"), sep=", "),
        sep=" ",
    )


def _enriched_contact(c: dict[str, Any]) -> str:
    emails = [_email_text(e) for e in _as_list(c.get("emails")) if isinstance(e, dict)]
    phones = [_phone_text(p) for p in _as_list(c.get("phones")) if isinstance(p, dict)]
    missing = [
        _join(m.get("dataPoint") or m.get("field"), m.get("status"), sep=" ")
        for m in _as_list(c.get("missingDataPoints"))
        if isinstance(m, dict)
    ]
    return _join(
        _contact_name(c),
        _contact_role(c),
        _location(c.get("location")),
        f"emails: {'; '.join(emails)}" if emails else "",
        f"phones: {'; '.join(phones)}" if phones else "",
        _as_dict(c.get("socialLinks")).get("linkedin"),
        f"not found: {', '.join(missing)}" if missing else "",
        "partial profile" if c.get("partialProfile") else "",
        _item_error(c),
        f"id={c.get('id', '?')}",
    )


_COMPANY_KEYS = (
    "id",
    "name",
    "domain",
    "description",
    "companyType",
    "yearFounded",
    "employeeCount",
    "revenueRange",
    "industry",
    "subIndustry",
    "location",
    "phone",
    "email",
    "socialLinks",
    "linkedinFollowers",
    "funding",
    "technologies",
    "intent",
    "openJobs",
    "estimatedAnnualItSpend",
    "monthlyWebsiteTraffic",
    "employeesByDepartment",
    "employeesByLocation",
    "employeesBySeniority",
    "competitors",
    "directParent",
    "ultimateParent",
    "businessModel",
    "sicCodes",
    "naicsCodes",
    "keywords",
    "error",
)


def _enriched_company(o: dict[str, Any]) -> str:
    view: dict[str, Any] = {k: o.get(k) for k in _COMPANY_KEYS}
    for key, cap in (("technologies", 30), ("keywords", 15), ("competitors", 15)):
        if isinstance(view.get(key), list):
            view[key] = view[key][:cap]
    return _dump(view, f"Company {o.get('name') or o.get('id') or '?'}", limit=3000)


def _enrich_status(data: Any) -> str:
    """The Data Waterfall status line and the async job, if Lusha left one."""
    data = _as_dict(data)
    out = []
    if data.get("status"):
        out.append(
            "Waterfall status: "
            + _join(
                data.get("status"),
                data.get("statusReason"),
                data.get("statusDescription"),
                sep=", ",
            )
        )
    job = _as_dict(data.get("job"))
    if job.get("id"):
        out.append(
            f"More data is still being looked up by third-party providers: job_id={job['id']}. "
            f"Read it with lusha_get_enrich_job after about {job.get('retryAfter', 5)}s "
            "(polling is free); give the user what is above now."
        )
    return "\n".join(out)


# ── Errors are results ──────────────────────────────────────────────────────


def _text_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """Make a tool return its failures as text the agent can act on, never raise.

    ``_request`` already turns every HTTP and transport failure into a
    ``[tool-error]`` string; this catches the rest (a Lusha response in a shape
    the formatter did not expect, a malformed argument) so the agent always
    gets a message that says what happened and what to do next.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # deliberate catch-all: errors go back as text
            logger.warning("%s failed: %s", fn.__name__, exc, exc_info=True)
            return (
                f"[tool-error] {fn.__name__}: {type(exc).__name__}: {str(exc)[:300]}. "
                "Lusha may already have applied a write (or charged credits) before this "
                "failed, so check with a list or get call before retrying one; for a read, "
                "retry with simpler arguments or use lusha_api_request to see the raw answer."
            )

    return wrapper


# ── Search and enrich: contacts ─────────────────────────────────────────────

_CONTACTS_JSON_HELP = (
    "JSON array of up to 100 contacts, each with one identifier: id, linkedinUrl, email, "
    "or firstName + lastName + (companyName or companyDomain); optional clientReferenceId "
    'is echoed back. e.g. \'[{"email": "a@acme.com"}, {"firstName": "Dana", "lastName": '
    '"Lee", "companyDomain": "acme.com"}]\'. Or leave empty and use the single-contact fields.'
)


def _one_contact(
    contact_id: str,
    linkedin_url: str,
    email: str,
    first_name: str,
    last_name: str,
    company_name: str,
    company_domain: str,
) -> dict[str, Any]:
    return {
        "id": contact_id.strip(),
        "linkedinUrl": linkedin_url.strip(),
        "email": email.strip(),
        "firstName": first_name.strip(),
        "lastName": last_name.strip(),
        "companyName": company_name.strip(),
        "companyDomain": company_domain.strip(),
    }


def _signal_filter(types: str, start_date: str, max_per_signal: int) -> dict[str, Any]:
    if not _csv(types):
        return {}
    return _clean(
        {
            "types": _csv(types),
            "startDate": start_date.strip(),
            "maxResultsPerSignal": min(max(int(max_per_signal), 1), 100)
            if max_per_signal
            else None,
        }
    )


@tool
@_text_errors
def lusha_search_contacts(
    contacts_json: Annotated[str, _CONTACTS_JSON_HELP] = "",
    contact_id: Annotated[str, "Lusha contact id"] = "",
    linkedin_url: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company_name: str = "",
    company_domain: Annotated[str, "Employer domain, e.g. 'acme.com'"] = "",
    signal_types: Annotated[
        str,
        "Only return contacts with these recent signals, comma-separated: promotion, "
        "companyChange, allSignals (lusha_get_signal_types kind='contacts')",
    ] = "",
    signals_start_date: Annotated[str, "YYYY-MM-DD; signals on or after this date"] = "",
    include_partial_profiles: bool = False,
) -> str:
    """Look up known people in Lusha and preview their profiles (no emails or phones).

    Each result shows the Lusha contact id, title, employer, what data Lusha
    has ("has") and what lusha_enrich_contacts can reveal with its credit
    price ("can reveal"). Charges the account's search action per result found.
    To find NEW people by filters (title, seniority, company size), use
    lusha_prospect_contacts instead.
    """
    contacts, err = _records(
        "contacts_json",
        contacts_json,
        _one_contact(
            contact_id, linkedin_url, email, first_name, last_name, company_name, company_domain
        ),
    )
    if err:
        return err
    body: dict[str, Any] = {"contacts": contacts}
    signals = _signal_filter(signal_types, signals_start_date, 0)
    if signals:
        body["signals"] = signals
    if include_partial_profiles:
        body["options"] = {"includePartialProfiles": True}
    data, err = _request("POST", "/v3/contacts/search", body=body)
    if err:
        return err
    return _rows(data.get("results"), _contact_line, "Contacts", data)


@tool
@_text_errors
def lusha_enrich_contacts(
    contact_ids: Annotated[str, "Comma-separated Lusha contact ids (up to 100) from a search"],
    reveal: Annotated[
        str, "Comma-separated: emails, phones. Empty reveals both (each is charged per contact)"
    ] = "",
    waterfall_enabled: Annotated[
        bool,
        "Let Lusha fall through to the account's enabled third-party providers when it has "
        "no match (only when Data Waterfall is set up in the account). False opts this call out.",
    ] = True,
    table_id: Annotated[str, "Also add these contacts to this contacts table"] = "",
) -> str:
    """Reveal emails and phone numbers for contacts found by a search or prospecting tool.

    Charges per revealed field per contact (the "can reveal" prices in the
    search result). A third-party waterfall lookup may finish later: the result
    then names a job_id to read with lusha_get_enrich_job.
    """
    ids, err = _ids("contact_ids", contact_ids)
    if err:
        return err
    fields = [f.lower() for f in _csv(reveal)]
    if any(f not in ("emails", "phones") for f in fields):
        return "[tool-error] reveal takes 'emails', 'phones' or both."
    body = _clean({"ids": ids, "reveal": fields, "tableId": table_id.strip()})
    if not waterfall_enabled:
        body["waterfallEnabled"] = False
    data, err = _request("POST", "/v3/contacts/enrich", body=body)
    if err:
        return err
    out = _rows(data.get("results"), _enriched_contact, "Enriched contacts", data)
    extra = _enrich_status(data)
    return f"{out}\n{extra}" if extra else out


@tool
@_text_errors
def lusha_search_and_enrich_contacts(
    contacts_json: Annotated[str, _CONTACTS_JSON_HELP] = "",
    contact_id: str = "",
    linkedin_url: str = "",
    email: str = "",
    first_name: str = "",
    last_name: str = "",
    company_name: str = "",
    company_domain: str = "",
    reveal: Annotated[str, "Comma-separated: emails, phones. Empty reveals both"] = "",
    include_partial_profiles: bool = False,
) -> str:
    """Find known people and reveal their emails and phones in one call.

    Charges the search per result plus each revealed field. Use it when the
    user already wants the contact details; use lusha_search_contacts first
    when they only want to see who matches.
    """
    contacts, err = _records(
        "contacts_json",
        contacts_json,
        _one_contact(
            contact_id, linkedin_url, email, first_name, last_name, company_name, company_domain
        ),
    )
    if err:
        return err
    fields = [f.lower() for f in _csv(reveal)]
    if any(f not in ("emails", "phones") for f in fields):
        return "[tool-error] reveal takes 'emails', 'phones' or both."
    body: dict[str, Any] = _clean({"contacts": contacts, "reveal": fields})
    if include_partial_profiles:
        body["options"] = {"includePartialProfiles": True}
    data, err = _request("POST", "/v3/contacts/search-and-enrich", body=body)
    if err:
        return err
    out = _rows(data.get("results"), _enriched_contact, "Enriched contacts", data)
    extra = _enrich_status(data)
    return f"{out}\n{extra}" if extra else out


@tool
@_text_errors
def lusha_get_enrich_job(
    job_id: Annotated[str, "job_id named by lusha_enrich_contacts"],
) -> str:
    """Read the rest of a contact enrichment that third-party providers were still working on.

    Free. While the job is processing, check again after the wait it names; on
    complete, the contacts listed here add to what the enrichment returned.
    """
    jid, err = _seg("job_id", job_id)
    if err:
        return err
    data, err = _request("GET", f"/v3/contacts/jobs/{jid}")
    if err:
        if "HTTP 404" in err:
            return (
                f"Job {jid} no longer exists (expired, or it belongs to another account). "
                "Stop polling; keep what the enrichment already returned."
            )
        return err
    data = _as_dict(data)
    status = data.get("status") or "?"
    if status == "processing":
        covers = [
            _join(c.get("contactId"), ", ".join(map(str, c.get("dataPoints") or [])), sep=": ")
            for c in _as_list(data.get("covers"))
            if isinstance(c, dict)
        ]
        return (
            f"Job {jid} is still processing (not an error). Check again in "
            f"{data.get('retryAfter', 5)}s. Outstanding: {'; '.join(covers) or 'unknown'}."
        )
    if status != "complete":
        return f"Job {jid} ended with status {status}{': ' + str(data['reason']) if data.get('reason') else ''}."
    out = _rows(data.get("contacts"), _enriched_contact, f"Job {jid} complete; contacts")
    if data.get("creditsCharged") is not None:
        out += f"\nCredits charged by the job: {data['creditsCharged']}."
    return out


# ── Search and enrich: companies ────────────────────────────────────────────

_COMPANIES_JSON_HELP = (
    "JSON array of up to 100 companies, each with id, domain or name; optional "
    'clientReferenceId is echoed back. e.g. \'[{"domain": "acme.com"}, {"name": "Globex"}]\'. '
    "Or leave empty and use company_id / domain / name for one company."
)
_COMPANY_REVEALS = (
    "employeesByDepartment",
    "employeesByLocation",
    "employeesBySeniority",
    "competitors",
    "intent",
    "estimatedAnnualItSpend",
    "monthlyWebsiteTraffic",
    "openJobsTotal",
    "openJobsByDepartment",
    "openJobsByLocation",
    "openJobsBySeniority",
    "directParent",
    "ultimateParent",
)
_COMPANY_REVEAL_HELP = "Comma-separated premium fields, each charged per company: " + ", ".join(
    _COMPANY_REVEALS
)


def _company_reveals(value: str) -> tuple[list[str], str | None]:
    by_lower = {r.lower(): r for r in _COMPANY_REVEALS}
    out = []
    for f in _csv(value):
        if f.lower() not in by_lower:
            return (
                [],
                f"[tool-error] reveal field {f!r} is not one of: {', '.join(_COMPANY_REVEALS)}.",
            )
        out.append(by_lower[f.lower()])
    return out, None


@tool
@_text_errors
def lusha_search_companies(
    companies_json: Annotated[str, _COMPANIES_JSON_HELP] = "",
    company_id: Annotated[str, "Lusha company id"] = "",
    domain: str = "",
    name: str = "",
    signal_types: Annotated[
        str,
        "Only return companies with these recent signals, comma-separated, e.g. "
        "'headcountIncrease3m, surgeInHiring' (lusha_get_signal_types kind='companies')",
    ] = "",
    signals_start_date: Annotated[str, "YYYY-MM-DD"] = "",
    include_partial_profiles: bool = False,
) -> str:
    """Look up known companies in Lusha and preview them (industry, size, location).

    Each result shows the Lusha company id, what Lusha has on it and which
    premium fields lusha_enrich_companies can reveal at what price. Charges the
    search action per result found. To find NEW companies by filters, use
    lusha_prospect_companies.
    """
    companies, err = _records(
        "companies_json",
        companies_json,
        {"id": company_id.strip(), "domain": domain.strip(), "name": name.strip()},
    )
    if err:
        return err
    body: dict[str, Any] = {"companies": companies}
    signals = _signal_filter(signal_types, signals_start_date, 0)
    if signals:
        body["signals"] = signals
    if include_partial_profiles:
        body["options"] = {"includePartialProfiles": True}
    data, err = _request("POST", "/v3/companies/search", body=body)
    if err:
        return err
    return _rows(data.get("results"), _company_line, "Companies", data)


@tool
@_text_errors
def lusha_enrich_companies(
    company_ids: Annotated[str, "Comma-separated Lusha company ids (up to 100) from a search"],
    reveal: Annotated[str, _COMPANY_REVEAL_HELP] = "",
    table_id: Annotated[str, "Also add these companies to this companies table"] = "",
) -> str:
    """Reveal full company data: firmographics, revenue, funding, technologies, locations,
    social links and open job counts, plus any premium fields named in reveal.

    Charges the company reveal per company found, plus each premium field.
    """
    ids, err = _ids("company_ids", company_ids)
    if err:
        return err
    fields, err = _company_reveals(reveal)
    if err:
        return err
    body = _clean({"ids": ids, "reveal": fields, "tableId": table_id.strip()})
    data, err = _request("POST", "/v3/companies/enrich", body=body)
    if err:
        return err
    results = [r for r in _as_list(data.get("results")) if isinstance(r, dict)]
    if not results:
        return _with_billing("Lusha returned no companies.", data)
    out = "\n\n".join(_enriched_company(r) for r in results)
    return _with_billing(_clip(out, 20000), data)


@tool
@_text_errors
def lusha_search_and_enrich_companies(
    companies_json: Annotated[str, _COMPANIES_JSON_HELP] = "",
    company_id: str = "",
    domain: str = "",
    name: str = "",
    reveal: Annotated[str, _COMPANY_REVEAL_HELP] = "",
    include_partial_profiles: bool = False,
) -> str:
    """Find known companies and reveal their full data in one call (same charges as enrich)."""
    companies, err = _records(
        "companies_json",
        companies_json,
        {"id": company_id.strip(), "domain": domain.strip(), "name": name.strip()},
    )
    if err:
        return err
    fields, err = _company_reveals(reveal)
    if err:
        return err
    body: dict[str, Any] = _clean({"companies": companies, "reveal": fields})
    if include_partial_profiles:
        body["options"] = {"includePartialProfiles": True}
    data, err = _request("POST", "/v3/companies/search-and-enrich", body=body)
    if err:
        return err
    results = [r for r in _as_list(data.get("results")) if isinstance(r, dict)]
    if not results:
        return _with_billing("Lusha found no matching companies.", data)
    return _with_billing(_clip("\n\n".join(_enriched_company(r) for r in results), 20000), data)


# ── Prospecting ─────────────────────────────────────────────────────────────

_FILTERS_JSON_HELP = (
    "Optional JSON merged over the filters built from the other arguments, in Lusha's "
    'shape: {"contacts": {"include": {...}, "exclude": {...}}, "companies": {"include": '
    '{...}, "exclude": {...}}}. Use it for anything without an argument here: exclusions, '
    'cities ({"locations": [{"city": "Austin", "country": "United States"}]}), '
    "funding, founded year, SIC/NAICS codes, skills, education."
)


def _page(page: int, cap: int = 1000) -> int:
    return min(max(int(page or 0), 0), cap)


def _size(size: int, cap: int = 100, default: int = 25) -> int:
    return min(max(int(size or default), 1), cap)


def _company_include(
    *,
    domains: str = "",
    names: str = "",
    countries: str = "",
    sizes: str = "",
    revenues: str = "",
    technologies: str = "",
    intent_topics: str = "",
    keywords: str = "",
    main_industry_ids: str = "",
) -> dict[str, Any]:
    return _clean(
        {
            "domains": _csv(domains),
            "names": _csv(names),
            "locations": [{"country": c} for c in _csv(countries)],
            "sizes": _ranges(sizes),
            "revenues": _ranges(revenues),
            "technologies": _csv(technologies),
            "intentTopics": _csv(intent_topics),
            "keywords": _csv(keywords),
            "mainIndustriesIds": _ints(main_industry_ids),
        }
    )


@tool
@_text_errors
def lusha_prospect_contacts(
    job_titles: Annotated[str, "Comma-separated job titles, e.g. 'VP Sales, Head of Revenue'"] = "",
    seniority_ids: Annotated[
        str,
        "Comma-separated seniority ids (lusha_get_prospecting_filters kind='contacts' filter_type='seniority')",
    ] = "",
    departments: Annotated[str, "Comma-separated departments, e.g. 'Sales, Marketing'"] = "",
    countries: Annotated[
        str, "Comma-separated countries the people are in, e.g. 'United States, Germany'"
    ] = "",
    existing_data_points: Annotated[
        str,
        "Only people Lusha holds these for, e.g. 'work_email, mobile_phone' (filter_type='existingDataPoints')",
    ] = "",
    search_text: Annotated[str, "Free text matched against the contact profile"] = "",
    company_domains: Annotated[str, "Comma-separated employer domains"] = "",
    company_names: Annotated[str, "Comma-separated employer names"] = "",
    company_countries: Annotated[str, "Comma-separated employer HQ countries"] = "",
    company_sizes: Annotated[
        str, "Employee ranges 'min-max' joined by ';', e.g. '51-200; 201-500; 5000-'"
    ] = "",
    company_revenues: Annotated[
        str, "Revenue ranges in USD 'min-max' joined by ';', e.g. '1000000-10000000'"
    ] = "",
    company_industries: Annotated[
        str, "Comma-separated industry labels (filter_type='industriesLabels')"
    ] = "",
    company_technologies: Annotated[str, "Comma-separated technologies the employer uses"] = "",
    company_intent_topics: Annotated[str, "Comma-separated buyer intent topics"] = "",
    signal_types: Annotated[str, "Only people with these signals: promotion, companyChange"] = "",
    signals_start_date: Annotated[str, "YYYY-MM-DD"] = "",
    filters_json: Annotated[str, _FILTERS_JSON_HELP] = "",
    max_contacts_per_company: Annotated[int, "1-20; 0 for no cap"] = 0,
    exclude_do_not_call: bool = False,
    include_partial_profiles: bool = False,
    table_id: Annotated[str, "Also save the results into this contacts table"] = "",
    page: Annotated[int, "0-based page number"] = 0,
    page_size: Annotated[int, "Results per page, 1-100"] = 25,
) -> str:
    """Find new prospects in Lusha's database by role and company filters (previews only).

    Returns names, titles, employers and Lusha contact ids, never emails or
    phones: pass the ids to lusha_enrich_contacts to reveal them. Charges the
    search action per result returned (and per matched signal when filtering by
    signals). Discover valid filter values with lusha_get_prospecting_filters.
    """
    extra, err = _json_object("filters_json", filters_json)
    if err:
        return err
    person = _clean(
        {
            "jobTitles": _csv(job_titles),
            "seniorityIds": _ints(seniority_ids),
            "departments": _csv(departments),
            "locations": [{"country": c} for c in _csv(countries)],
            "existingDataPoints": _csv(existing_data_points),
            "searchText": search_text.strip(),
            "signals": _clean(
                {"types": _csv(signal_types), "startDate": signals_start_date.strip()}
            )
            if _csv(signal_types)
            else None,
        }
    )
    company = _company_include(
        domains=company_domains,
        names=company_names,
        countries=company_countries,
        sizes=company_sizes,
        revenues=company_revenues,
        technologies=company_technologies,
        intent_topics=company_intent_topics,
    )
    if _csv(company_industries):
        company["industriesLabels"] = _csv(company_industries)
    filters = _merge(
        _clean(
            {
                "contacts": {"include": person} if person else None,
                "companies": {"include": company} if company else None,
            }
        ),
        extra,
    )
    if not filters:
        return "[tool-error] Give at least one filter (a title, department, company, country, ...)."
    options = _clean(
        {
            "maxContactsPerCompany": min(max(int(max_contacts_per_company), 1), 20)
            if max_contacts_per_company
            else None,
            "excludeDnc": True if exclude_do_not_call else None,
            "includePartialProfiles": True if include_partial_profiles else None,
        }
    )
    body = _clean(
        {
            "pagination": {"page": _page(page), "size": _size(page_size)},
            "filters": filters,
            "options": options,
            "tableId": table_id.strip(),
        }
    )
    data, err = _request("POST", "/v3/contacts/prospecting", body=body)
    if err:
        return err
    return _rows(data.get("results"), _contact_line, "Prospects", data)


@tool
@_text_errors
def lusha_prospect_companies(
    search_text: Annotated[str, "Free text matched against the company profile"] = "",
    domains: Annotated[str, "Comma-separated domains"] = "",
    names: Annotated[str, "Comma-separated company names"] = "",
    countries: Annotated[str, "Comma-separated HQ countries"] = "",
    sizes: Annotated[str, "Employee ranges 'min-max' joined by ';', e.g. '51-200; 201-500'"] = "",
    revenues: Annotated[str, "Revenue ranges in USD 'min-max' joined by ';'"] = "",
    main_industry_ids: Annotated[
        str,
        "Comma-separated industry ids (lusha_get_prospecting_filters kind='companies' filter_type='industriesLabels')",
    ] = "",
    technologies: Annotated[
        str, "Comma-separated technologies in use, e.g. 'Salesforce, React'"
    ] = "",
    intent_topics: Annotated[str, "Comma-separated buyer intent topics"] = "",
    keywords: Annotated[str, "Comma-separated company keywords, e.g. 'fintech, payments'"] = "",
    signal_types: Annotated[
        str,
        "Only companies with these signals, e.g. 'headcountIncrease3m, surgeInHiring, "
        "financialEventsNews' (lusha_get_signal_types kind='companies')",
    ] = "",
    signals_start_date: Annotated[str, "YYYY-MM-DD"] = "",
    filters_json: Annotated[str, _FILTERS_JSON_HELP] = "",
    include_partial_profiles: bool = False,
    table_id: Annotated[str, "Also save the results into this companies table"] = "",
    page: Annotated[int, "0-based page number"] = 0,
    page_size: Annotated[int, "Results per page, 1-100"] = 25,
) -> str:
    """Find companies in Lusha's database by size, revenue, industry, technology, intent
    and signal filters (previews; lusha_enrich_companies reveals the full data).

    Charges the search action per result returned (and per matched signal when
    filtering by signals). Contact filters (filters_json "contacts") cannot be
    combined with search_text, signals, intent topics or technologies.
    """
    extra, err = _json_object("filters_json", filters_json)
    if err:
        return err
    company = _company_include(
        domains=domains,
        names=names,
        countries=countries,
        sizes=sizes,
        revenues=revenues,
        technologies=technologies,
        intent_topics=intent_topics,
        keywords=keywords,
        main_industry_ids=main_industry_ids,
    )
    if search_text.strip():
        company["searchText"] = search_text.strip()
    if _csv(signal_types):
        company["signals"] = _clean(
            {"types": _csv(signal_types), "startDate": signals_start_date.strip()}
        )
    filters = _merge({"companies": {"include": company}}, extra)
    if not _as_dict(_as_dict(filters.get("companies")).get("include")):
        return "[tool-error] Give at least one company filter (size, country, industry, ...)."
    body = _clean(
        {
            "pagination": {"page": _page(page), "size": _size(page_size)},
            "filters": filters,
            "options": {"includePartialProfiles": True} if include_partial_profiles else None,
            "tableId": table_id.strip(),
        }
    )
    data, err = _request("POST", "/v3/companies/prospecting", body=body)
    if err:
        return err
    return _rows(data.get("results"), _company_line, "Companies", data)


_CONTACT_FILTERS = ("departments", "seniority", "existingDataPoints", "countries", "locations")
_COMPANY_FILTERS = (
    "names",
    "sizes",
    "revenues",
    "locations",
    "sics",
    "naics",
    "industriesLabels",
    "intentTopics",
    "technologies",
)


@tool
@_text_errors
def lusha_get_prospecting_filters(
    kind: Annotated[str, "'contacts' or 'companies'"],
    filter_type: Annotated[
        str,
        "Empty lists the filter types. Contacts: departments, seniority, existingDataPoints, "
        "countries, locations. Companies: names, sizes, revenues, locations, sics, naics, "
        "industriesLabels, intentTopics, technologies",
    ] = "",
    query: Annotated[
        str, "Text to match (2-256 chars); required for locations, names and technologies"
    ] = "",
) -> str:
    """List the valid values for a prospecting filter (seniority ids, industry ids, ...). Free."""
    k, err = _kind(kind)
    if err:
        return err
    base = f"/v3/{k}/prospecting/filters"
    ftype = filter_type.strip()
    if not ftype:
        data, err = _request("GET", base)
        if err:
            return err
        return _rows(
            data.get("availableFilters"),
            lambda f: _join(f.get("filterType"), "needs query" if f.get("requiresQuery") else ""),
            f"{k.title()} prospecting filters",
        )
    allowed = _CONTACT_FILTERS if k == "contacts" else _COMPANY_FILTERS
    if ftype not in allowed:
        return f"[tool-error] filter_type for {k} must be one of: {', '.join(allowed)}."
    data, err = _request("GET", f"{base}/{ftype}", params=_clean({"query": query.strip()}))
    if err:
        return err
    values = _as_list(_as_dict(data).get("values"))
    if not values:
        return f"No values for {k} filter {ftype}{' matching ' + repr(query) if query else ''}."
    lines = [f"  {_compact(v) if not isinstance(v, str) else v}" for v in values]
    return _clip(f"Values for {k} filter {ftype} ({len(values)}):\n" + "\n".join(lines))


# ── Lookalikes and buying groups ────────────────────────────────────────────


def _lookalike_body(
    seeds: dict[str, Any],
    exclude: dict[str, Any],
    limit: int,
    session_id: str,
    table_id: str,
) -> tuple[dict[str, Any], str | None]:
    total = sum(len(v) for v in seeds.values() if isinstance(v, list))
    if total < 5 or total > 100:
        return {}, f"[tool-error] Lusha needs 5 to 100 seeds in total; {total} given."
    return (
        _clean(
            {
                "seeds": seeds,
                "exclude": _clean(exclude) or None,
                "limit": _size(limit, 100, 25),
                "dedupeSessionId": session_id.strip(),
                "tableId": table_id.strip(),
            }
        ),
        None,
    )


def _lookalike_out(data: Any, line: Callable[[dict[str, Any]], str], heading: str) -> str:
    data = _as_dict(data)
    out = _rows(data.get("results"), line, heading, data)
    meta = _as_dict(data.get("meta"))
    if data.get("dedupeSessionId"):
        more = " Lusha has more." if meta.get("hasMore") else ""
        out += (
            f"\nsession_id={data['dedupeSessionId']}: pass it back for the next batch without "
            f"repeats.{more}"
        )
    return out


@tool
@_text_errors
def lusha_find_contact_lookalikes(
    seed_linkedin_urls: Annotated[
        str, "Comma-separated LinkedIn profile URLs of example people"
    ] = "",
    seed_emails: str = "",
    seed_ids: Annotated[str, "Comma-separated Lusha contact ids"] = "",
    seed_contacts_json: Annotated[
        str, 'JSON array of {"firstName", "lastName", "companyDomain" or "companyName"}'
    ] = "",
    exclude_linkedin_urls: str = "",
    exclude_emails: str = "",
    exclude_ids: str = "",
    limit: Annotated[int, "Results to return, 1-100"] = 25,
    session_id: Annotated[
        str, "session_id from a previous call, for the next batch without repeats"
    ] = "",
    table_id: Annotated[str, "Also save the results into this contacts table"] = "",
) -> str:
    """Find people similar to 5-100 example people (role, seniority, company profile).

    Results are previews; enrich the ids to reveal contact details. Charges per
    result returned.
    """
    named, err = _loads("seed_contacts_json", seed_contacts_json)
    if err:
        return err
    if named is not None and not (
        isinstance(named, list) and all(isinstance(c, dict) for c in named)
    ):
        return "[tool-error] seed_contacts_json must be a JSON array of objects."
    seeds = _clean(
        {
            "linkedinUrls": _csv(seed_linkedin_urls),
            "emails": _csv(seed_emails),
            "ids": _csv(seed_ids),
            "contacts": named or [],
        }
    )
    exclude = {
        "linkedinUrls": _csv(exclude_linkedin_urls),
        "emails": _csv(exclude_emails),
        "ids": _csv(exclude_ids),
    }
    body, err = _lookalike_body(seeds, exclude, limit, session_id, table_id)
    if err:
        return err
    data, err = _request("POST", "/v3/contacts/lookalike", body=body)
    if err:
        return err
    return _lookalike_out(data, _contact_line, "Lookalike contacts")


@tool
@_text_errors
def lusha_find_company_lookalikes(
    seed_domains: Annotated[
        str, "Comma-separated domains of example companies (e.g. best customers)"
    ] = "",
    seed_linkedin_urls: Annotated[str, "Comma-separated LinkedIn company URLs"] = "",
    exclude_domains: Annotated[
        str, "Comma-separated domains never to return (customers, competitors)"
    ] = "",
    exclude_linkedin_urls: str = "",
    limit: Annotated[int, "Results to return, 1-100"] = 25,
    session_id: Annotated[
        str, "session_id from a previous call, for the next batch without repeats"
    ] = "",
    table_id: Annotated[str, "Also save the results into this companies table"] = "",
) -> str:
    """Find companies similar to 5-100 example companies (size, industry, profile).

    Results are previews; lusha_enrich_companies reveals the full data. Charges
    per result returned.
    """
    seeds = _clean({"domains": _csv(seed_domains), "linkedinUrls": _csv(seed_linkedin_urls)})
    exclude = {"domains": _csv(exclude_domains), "linkedinUrls": _csv(exclude_linkedin_urls)}
    body, err = _lookalike_body(seeds, exclude, limit, session_id, table_id)
    if err:
        return err
    data, err = _request("POST", "/v3/companies/lookalike", body=body)
    if err:
        return err
    return _lookalike_out(data, _company_line, "Lookalike companies")


_PERSONAS = ("decision_maker", "potential_champion", "end_user")


@tool
@_text_errors
def lusha_get_buying_group(
    company_domains: Annotated[
        str, "Comma-separated company domains (up to 25 companies in all)"
    ] = "",
    company_ids: Annotated[str, "Comma-separated Lusha company ids"] = "",
    personas: Annotated[
        str,
        "Comma-separated: decision_maker, potential_champion, end_user. Empty returns all three",
    ] = "",
    contacts_per_company: Annotated[int, "Cap per company (Lusha's default is 60)"] = 0,
    page: Annotated[int, "0-based page number"] = 0,
    page_size: Annotated[int, "1-100"] = 100,
) -> str:
    """Identify the buying committee at target companies: decision makers, potential
    champions and end users, each with a relevance score (previews; enrich to reveal).
    """
    companies = [{"domain": d} for d in _csv(company_domains)] + [
        {"id": i} for i in _csv(company_ids)
    ]
    if not companies:
        return "[tool-error] Give at least one company domain or id."
    if len(companies) > 25:
        return (
            "[tool-error] Lusha takes at most 25 companies per buying-group call; split the list."
        )
    roles = [p.lower() for p in _csv(personas)]
    if any(p not in _PERSONAS for p in roles):
        return f"[tool-error] personas must be among: {', '.join(_PERSONAS)}."
    body = _clean(
        {
            "companies": companies,
            "personas": roles,
            "contactsLimit": int(contacts_per_company) if contacts_per_company else None,
            "pagination": {"page": _page(page), "size": _size(page_size, 100, 100)},
        }
    )
    data, err = _request("POST", "/v3/contacts/buying-group", body=body)
    if err:
        return err
    blocks = []
    for group in _as_list(_as_dict(data).get("results")):
        if not isinstance(group, dict):
            continue
        head = _join(
            f"Company {group.get('companyId', '?')}",
            group.get("clientReferenceId"),
            _item_error(group),
        )
        members = [
            f"    {_contact_line(c)}"
            for c in _as_list(group.get("contacts"))
            if isinstance(c, dict)
        ]
        blocks.append("\n".join([head + ":"] + (members or ["    (no contacts)"])))
    if not blocks:
        return _with_billing("Lusha found no buying group contacts.", data)
    return _with_billing(_clip("\n".join(blocks)), data)


# ── Signals ─────────────────────────────────────────────────────────────────


def _signals_out(data: Any, heading: str) -> str:
    data = _as_dict(data)
    blocks = []
    for item in _as_list(data.get("results")):
        if not isinstance(item, dict):
            continue
        head = _join(
            item.get("companyName") or item.get("name"),
            item.get("domain"),
            f"id={item.get('id', '?')}",
            _item_error(item),
        )
        lines = [head + ":"]
        for key, events in item.items():
            if isinstance(events, list) and events:
                lines.append(
                    f"    {key} ({len(events)}): " + "; ".join(_compact(e, 200) for e in events[:5])
                )
        if len(lines) == 1:
            lines.append("    no signals in the window")
        blocks.append("\n".join(lines))
    window = _join(data.get("startDate"), data.get("endDate"), sep=" to ")
    head = f"{heading}{' (' + window + ')' if window else ''}"
    if not blocks:
        return _with_billing(f"{head}: none.", data)
    return _with_billing(_clip(head + ":\n" + "\n".join(blocks)), data)


@tool
@_text_errors
def lusha_get_contact_signals(
    contact_ids: Annotated[str, "Comma-separated Lusha contact ids (up to 100)"],
    signal_types: Annotated[
        str, "Comma-separated: promotion, companyChange or allSignals"
    ] = "allSignals",
    start_date: Annotated[str, "YYYY-MM-DD; only events on or after it"] = "",
    max_results_per_signal: Annotated[int, "1-100; 0 returns all"] = 0,
    table_id: Annotated[
        str, "Also add these contacts to this table and fill its Signals column"
    ] = "",
) -> str:
    """Read job changes and promotions for contacts. Charges per matched signal."""
    ids, err = _ids("contact_ids", contact_ids)
    if err:
        return err
    body = _clean(
        {
            "ids": ids,
            "signalTypes": _csv(signal_types) or ["allSignals"],
            "startDate": start_date.strip(),
            "maxResultsPerSignal": min(max(int(max_results_per_signal), 1), 100)
            if max_results_per_signal
            else None,
            "tableId": table_id.strip(),
        }
    )
    data, err = _request("POST", "/v3/contacts/signals", body=body)
    if err:
        return err
    return _signals_out(data, "Contact signals")


@tool
@_text_errors
def lusha_get_company_signals(
    company_ids: Annotated[str, "Comma-separated Lusha company ids (up to 100)"],
    signal_types: Annotated[
        str,
        "Comma-separated signal types (lusha_get_signal_types kind='companies'), e.g. "
        "'surgeInHiring, headcountIncrease3m, financialEventsNews'. allSignals covers all "
        "but the four intent types (newIntentTopic, intentScoreIncrease, "
        "intentScoreDecrease, intentTopicRemoved), which must be named",
    ] = "allSignals",
    start_date: Annotated[str, "YYYY-MM-DD; only events on or after it"] = "",
    max_results_per_signal: Annotated[int, "1-100; 0 returns all"] = 0,
    intent_categories: Annotated[
        str, "Comma-separated intent categories to narrow intent signals"
    ] = "",
    intent_topics: Annotated[
        str,
        "Comma-separated intent topics (lusha_get_company_signal_filters filter_type='intentTopics')",
    ] = "",
    table_id: Annotated[
        str, "Also add these companies to this table and fill its Signals column"
    ] = "",
) -> str:
    """Read company signals: hiring surges, headcount and web-traffic changes, IT spend,
    news events, LinkedIn activity and buyer-intent changes. Charges per matched signal.
    """
    ids, err = _ids("company_ids", company_ids)
    if err:
        return err
    include = _clean(
        {"intentCategories": _csv(intent_categories), "intentTopics": _csv(intent_topics)}
    )
    body = _clean(
        {
            "ids": ids,
            "signalTypes": _csv(signal_types) or ["allSignals"],
            "startDate": start_date.strip(),
            "maxResultsPerSignal": min(max(int(max_results_per_signal), 1), 100)
            if max_results_per_signal
            else None,
            "filters": {"include": include} if include else None,
            "tableId": table_id.strip(),
        }
    )
    data, err = _request("POST", "/v3/companies/signals", body=body)
    if err:
        return err
    return _signals_out(data, "Company signals")


@tool
@_text_errors
def lusha_get_signal_types(kind: Annotated[str, "'contacts' or 'companies'"]) -> str:
    """List the signal types Lusha supports for contacts or companies. Free."""
    k, err = _kind(kind)
    if err:
        return err
    data, err = _request("GET", f"/v3/{k}/signals/types")
    if err:
        return err
    types = _as_list(_as_dict(data).get("signalTypes"))
    names = [t if isinstance(t, str) else _compact(t, 120) for t in types]
    return (
        f"{k.title()} signal types: {', '.join(names)}"
        if names
        else f"Lusha listed no {k} signal types."
    )


@tool
@_text_errors
def lusha_get_company_signal_filters(
    filter_type: Annotated[
        str,
        "Empty lists the filter types. Else: newsEventTypes, hiringByDepartments, "
        "hiringByLocations, intentCategories, intentTopics",
    ] = "",
    query: Annotated[
        str, "Text to match (2-256 chars); required for hiringByLocations and intentTopics"
    ] = "",
) -> str:
    """List the filter values usable with company signals (news event types, intent topics, ...). Free."""
    ftype = filter_type.strip()
    if not ftype:
        data, err = _request("GET", "/v3/companies/signals/filters")
        if err:
            return err
        return _rows(
            _as_dict(data).get("availableFilters"),
            lambda f: _join(f.get("filterType"), "needs query" if f.get("requiresQuery") else ""),
            "Company signal filters",
        )
    allowed = (
        "newsEventTypes",
        "hiringByDepartments",
        "hiringByLocations",
        "intentCategories",
        "intentTopics",
    )
    if ftype not in allowed:
        return f"[tool-error] filter_type must be one of: {', '.join(allowed)}."
    params = _clean({"query": query.strip()}) if ftype != "intentCategories" else None
    data, err = _request("GET", f"/v3/companies/signals/filters/{ftype}", params=params)
    if err:
        return err
    values = _as_list(_as_dict(data).get("values"))
    if not values:
        return f"No values for signal filter {ftype}{' matching ' + repr(query) if query else ''}."
    return _clip(
        f"Values for signal filter {ftype} ({len(values)}):\n"
        + "\n".join(f"  {v if isinstance(v, str) else _compact(v)}" for v in values)
    )


# ── Scores: signal activity and ICP fit ─────────────────────────────────────

_SCORE_RECORDS_HELP = (
    "JSON array of up to 100 records. Companies: each with exactly one of id, domain, name "
    "or email. Contacts: each with id, linkedinUrl, email, or firstName + lastName + "
    "(companyName or companyDomain). Optional clientReferenceId is echoed back."
)


def _score_line(r: dict[str, Any]) -> str:
    company = _as_dict(r.get("company"))
    who = r.get("fullName") or r.get("name") or company.get("name") or r.get("domain")
    top = _as_dict(r.get("topProfile"))
    per = [
        f"{p.get('name') or p.get('id')}={p.get('score')}"
        for p in _as_list(r.get("profileScores"))
        if isinstance(p, dict)
    ]
    return _join(
        who,
        r.get("domain") or company.get("domain"),
        f"signalScore={r['signalScore']}" if r.get("signalScore") is not None else "",
        "no active signals" if r.get("noActiveSignals") else "",
        f"signals: {', '.join(map(str, r['signalTypes']))}" if r.get("signalTypes") else "",
        f"icpFitScore={r['icpFitScore']}" if r.get("icpFitScore") is not None else "",
        f"best fit: {top.get('name') or top.get('id')}" if top else "",
        f"per profile: {', '.join(per)}" if per else "",
        f"ref={r['clientReferenceId']}" if r.get("clientReferenceId") else "",
        _item_error(r),
        f"id={r['id']}" if r.get("id") else "",
    )


@tool
@_text_errors
def lusha_score_by_signals(
    kind: Annotated[str, "'contacts' or 'companies'"],
    records_json: Annotated[str, _SCORE_RECORDS_HELP],
) -> str:
    """Score contacts or companies by their active buying signals (0-1, with the signal
    breakdown). Charges 1 credit per scored record where the account's plan prices it.
    """
    k, err = _kind(kind)
    if err:
        return err
    records, err = _records("records_json", records_json, {})
    if err:
        return err
    data, err = _request("POST", f"/v3/{k}/signal-score", body={k: records})
    if err:
        return err
    return _rows(data.get("results"), _score_line, f"Signal scores ({k})", data)


@tool
@_text_errors
def lusha_list_target_profiles(
    status: Annotated[str, "'active', 'disabled' or empty for both"] = "",
    access: Annotated[str, "'private', 'shared' or empty for both"] = "",
) -> str:
    """List the account's Target Profiles (ICPs), whose ids lusha_score_icp_fit needs. Free."""
    params = _clean({"status": status.strip().lower(), "access": access.strip().lower()})
    data, err = _request("GET", "/v3/icp/profiles", params=params)
    if err:
        return err
    return _rows(
        _as_dict(data).get("profiles"),
        lambda p: _join(p.get("name"), p.get("status"), p.get("access"), f"id={p.get('id', '?')}"),
        "Target profiles",
        data,
    )


@tool
@_text_errors
def lusha_score_icp_fit(
    kind: Annotated[str, "'contacts' or 'companies'"],
    target_profile_ids: Annotated[str, "Comma-separated ids of 1-3 active Target Profiles"],
    records_json: Annotated[str, _SCORE_RECORDS_HELP],
) -> str:
    """Score how well contacts or companies fit 1-3 Target Profiles (0-1 per profile).

    Charges 1 credit per scored record, whatever the score; records Lusha
    cannot resolve or score are not charged.
    """
    k, err = _kind(kind)
    if err:
        return err
    profiles = _csv(target_profile_ids)
    if not 1 <= len(profiles) <= 3:
        return "[tool-error] Give 1 to 3 target_profile_ids (lusha_list_target_profiles)."
    records, err = _records("records_json", records_json, {})
    if err:
        return err
    data, err = _request(
        "POST", f"/v3/{k}/icp-fit-score", body={"targetProfileIds": profiles, k: records}
    )
    if err:
        return err
    return _rows(data.get("results"), _score_line, f"ICP fit scores ({k})", data)


# ── Website visitors and conversations ──────────────────────────────────────


@tool
@_text_errors
def lusha_get_website_visitors(
    domains: Annotated[str, "Comma-separated website domains tracked in the Lusha dashboard"],
    start_date: Annotated[str, "YYYY-MM-DD"],
    end_date: Annotated[str, "YYYY-MM-DD; at most 3 months after start_date"],
    score_bands: Annotated[str, "Comma-separated bands to keep, e.g. 'hot, warm'"] = "",
    min_score: Annotated[int, "1-100; 0 for no minimum"] = 0,
    visitor_countries: Annotated[str, "Comma-separated ISO country codes, e.g. 'US, GB'"] = "",
    filters_json: Annotated[
        str,
        "Optional JSON merged over the filters, e.g. "
        '\'{"totalSessions": {"min": 3}, "daysSinceLastVisit": {"max": 14}}\'',
    ] = "",
    sort_by: Annotated[str, "Field to sort by, e.g. 'score'"] = "score",
    sort_order: Annotated[str, "'asc' or 'desc'"] = "desc",
    page: Annotated[int, "0-based page number"] = 0,
    page_size: Annotated[int, "1-150"] = 25,
) -> str:
    """List the companies visiting the account's tracked websites, ranked by visit intent.

    Every domain must be set up for tracking in the Lusha dashboard, or the
    whole request fails.
    """
    sites = _csv(domains)
    if not sites:
        return "[tool-error] Give at least one tracked domain."
    extra, err = _json_object("filters_json", filters_json)
    if err:
        return err
    filters = _merge(
        _clean(
            {
                "scoreBands": _csv(score_bands),
                "score": {"min": int(min_score)} if min_score else None,
                "visitorCountries": {"include": [c.upper() for c in _csv(visitor_countries)]}
                if _csv(visitor_countries)
                else None,
            }
        ),
        extra,
    )
    body = _clean(
        {
            "domains": sites,
            "startDate": start_date.strip(),
            "endDate": end_date.strip(),
            "pagination": {"page": _page(page), "size": _size(page_size, 150)},
            "sort": _clean({"by": sort_by.strip(), "order": sort_order.strip().lower()}),
            "filters": filters,
        }
    )

    def line(o: dict[str, Any]) -> str:
        visit = _join(
            f"score={o.get('score')}" if o.get("score") is not None else "",
            o.get("scoreBand"),
            f"sessions={o.get('totalSessions')}" if o.get("totalSessions") is not None else "",
            f"visitors={o.get('uniqueVisitors')}" if o.get("uniqueVisitors") is not None else "",
            f"last visit {o.get('lastVisit')}" if o.get("lastVisit") else "",
        )
        return _join(_company_line(o), visit)

    data, err = _request("POST", "/v3/companies/website-visits", body=body)
    if err:
        return err
    return _rows(data.get("results"), line, "Website visitors", data)


@tool
@_text_errors
def lusha_search_conversations(
    query: Annotated[
        str, "Free text searched in transcripts; when given, the other filters are ignored"
    ] = "",
    date_from: Annotated[str, "YYYY-MM-DD"] = "",
    date_to: Annotated[str, "YYYY-MM-DD"] = "",
    contact_names: Annotated[str, "Comma-separated participant names (partial match)"] = "",
    company_domains: Annotated[str, "Comma-separated domains of the external participants"] = "",
    meeting_titles: Annotated[str, "Comma-separated meeting title fragments"] = "",
    conversation_ids: Annotated[str, "Comma-separated conversation ids"] = "",
    page: Annotated[int, "1-based page number"] = 1,
    page_size: Annotated[int, "1-100"] = 10,
) -> str:
    """Search the sales calls recorded by Lusha Conversations: summary, action items,
    risks, objections, competitors and coaching for each (no transcript).
    """
    body = _clean(
        {
            "query": query.strip(),
            "dateFrom": date_from.strip(),
            "dateTo": date_to.strip(),
            "contactNames": _csv(contact_names),
            "companyDomains": _csv(company_domains),
            "meetingTitles": _csv(meeting_titles),
            "conversationIds": _csv(conversation_ids),
            "page": max(int(page or 1), 1),
            "pageSize": _size(page_size, 100, 10),
        }
    )
    data, err = _request("POST", "/v3/account/conversations/search", body=body)
    if err:
        return err
    convs = [c for c in _as_list(_as_dict(data).get("conversations")) if isinstance(c, dict)]
    if not convs:
        return _with_billing("No conversations found.", data)
    blocks = []
    for c in convs:
        summary = _as_dict(c.get("summary"))
        view = {
            "id": c.get("id"),
            "title": c.get("title"),
            "date": c.get("date"),
            "duration": c.get("durationFormatted"),
            "company": _as_dict(c.get("company")).get("domain")
            or _as_dict(c.get("company")).get("name"),
            "participants": [
                _join(
                    p.get("name"),
                    p.get("role"),
                    "internal" if p.get("isInternal") else "",
                    sep=", ",
                )
                for p in _as_list(c.get("participants"))
                if isinstance(p, dict)
            ],
            "summary": summary.get("text"),
            "actionItems": summary.get("actionItems"),
            "nextSteps": summary.get("nextSteps"),
            "risks": summary.get("risks"),
            "objections": summary.get("objections"),
            "competition": summary.get("competition"),
            "coaching": c.get("coaching"),
        }
        blocks.append(_dump(view, f"Conversation {c.get('title') or c.get('id')}", limit=2500))
    return _with_billing(_clip("\n\n".join(blocks), 20000), data)


@tool
@_text_errors
def lusha_get_conversation_transcript(
    conversation_id: Annotated[str, "id from lusha_search_conversations"],
    max_chars: Annotated[int, "Cut the transcript at this many characters"] = 20000,
) -> str:
    """Get the speaker-attributed transcript of one recorded conversation.

    Charges 1 credit per call where the plan prices it, repeats included, so
    prefer the summary from lusha_search_conversations when it is enough.
    """
    cid, err = _seg("conversation_id", conversation_id)
    if err:
        return err
    data, err = _request("GET", f"/v3/account/conversations/{cid}/transcript")
    if err:
        return err
    lines = []
    for s in _as_list(_as_dict(data).get("segments")):
        if not isinstance(s, dict):
            continue
        speaker = s.get("speaker") or "?"
        when = s.get("startTime")
        lines.append(
            f"[{when}] {speaker}: {s.get('text') or ''}"
            if when is not None
            else f"{speaker}: {s.get('text') or ''}"
        )
    if not lines:
        return _with_billing(f"Conversation {cid} has no transcript segments.", data)
    limit = max(int(max_chars or 20000), 1000)
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[:limit] + f"\n... (cut at {limit} characters; raise max_chars for more)"
    return _with_billing(f"Transcript {cid}:\n{text}", data)


# ── Tables ──────────────────────────────────────────────────────────────────

_OWNER_HELP = (
    "Email of a user on the Lusha account the key belongs to; Lusha acts on tables as that user"
)


def _table_line(t: dict[str, Any]) -> str:
    return _join(
        t.get("name") or "(no name)",
        t.get("entityType"),
        t.get("visibility"),
        t.get("status"),
        f"{t['entityCount']} rows" if t.get("entityCount") is not None else "",
        "processing" if t.get("isProcessing") else "",
        f"owner={_as_dict(t.get('owner')).get('email')}"
        if _as_dict(t.get("owner")).get("email")
        else "",
        f"id={t.get('tableId') or t.get('id') or '?'}",
    )


def _column_line(c: dict[str, Any]) -> str:
    counts = _join(
        *(
            f"{label}={c[key]}"
            for key, label in (
                ("rowsSuccess", "filled"),
                ("rowsNoData", "no data"),
                ("rowsProcessing", "running"),
                ("rowsNotRun", "not run"),
                ("rowsFailed", "failed"),
            )
            if c.get(key)
        ),
        sep=", ",
    )
    return _join(
        c.get("name") or c.get("displayName"),
        c.get("key") or c.get("columnKey"),
        c.get("type"),
        "default" if c.get("isDefault") else "",
        counts,
        f"id={c.get('columnId') or '?'}",
    )


def _cell(c: dict[str, Any]) -> str:
    value = c.get("value")
    if value in (None, "", [], {}):
        return f"{c.get('name') or c.get('id')}: ({c.get('status') or 'empty'})"
    text = value if isinstance(value, str) else _compact(value, 200)
    return f"{c.get('name') or c.get('id')}: {text}"


def _table_rows_out(data: Any, heading: str) -> str:
    data = _as_dict(data)
    rows = [r for r in _as_list(data.get("data")) if isinstance(r, dict)]
    if not rows:
        return _with_billing(f"{heading}: no rows.", data)
    pg = _as_dict(data.get("pagination"))
    meta = _join(
        f"page {pg.get('page')}" if pg.get("page") is not None else "",
        f"{pg.get('total')} total" if pg.get("total") is not None else "",
        sep=", ",
    )
    lines = [f"{heading}{' (' + meta + ')' if meta else ''}:"]
    for r in rows:
        cells = [_cell(c) for c in _as_list(r.get("columns")) if isinstance(c, dict)]
        lines.append(f"  id={r.get('id', '?')} | " + " | ".join(cells))
    return _with_billing(_clip("\n".join(lines)), data)


@tool
@_text_errors
def lusha_create_table(
    kind: Annotated[str, "'contacts' or 'companies'"],
    name: str,
    owner_email: Annotated[str, _OWNER_HELP],
    visibility: Annotated[str, "'private' or 'shared' (with the whole Lusha account)"] = "private",
    ids: Annotated[str, "Optional comma-separated Lusha ids to seed the table with"] = "",
) -> str:
    """Create a Lusha table to keep and enrich a list of contacts or companies. Free."""
    k, err = _kind(kind)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    vis = visibility.strip().lower()
    if vis not in ("private", "shared"):
        return "[tool-error] visibility must be 'private' or 'shared'."
    body = _clean({"name": name.strip(), "visibility": vis, "owner": owner, "ids": _csv(ids)})
    data, err = _request("POST", f"/v3/{k}/tables", body=body)
    if err:
        return err
    table = _as_dict(_as_dict(data).get("data"))
    return f"Created {k} table {_table_line(table) if table else name}."


@tool
@_text_errors
def lusha_list_tables(
    kind: Annotated[str, "'contacts' or 'companies'"],
    owner_email: Annotated[str, _OWNER_HELP],
    name: Annotated[str, "Only tables whose name contains this"] = "",
    status: Annotated[str, "'active', 'archived' or empty"] = "",
    page: Annotated[int, "0-based, up to 100"] = 0,
    page_size: int = 50,
) -> str:
    """List the user's contacts or companies tables, plus those shared with the account. Free."""
    k, err = _kind(kind)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    body = _clean(
        {
            "owner": owner,
            "name": name.strip(),
            "status": status.strip().lower(),
            "page": _page(page, 100),
            "size": _size(page_size, 100, 50),
        }
    )
    data, err = _request("POST", f"/v3/{k}/tables/list", body=body)
    if err:
        return err
    return _rows(_as_dict(data).get("data"), _table_line, f"{k.title()} tables", data)


@tool
@_text_errors
def lusha_get_table_column_catalog(kind: Annotated[str, "'contacts' or 'companies'"]) -> str:
    """List every Lusha data column that can be added to a contacts or companies table. Free."""
    k, err = _kind(kind)
    if err:
        return err
    data, err = _request("GET", f"/v3/{k}/tables/columns")
    if err:
        return err
    return _rows(
        _as_dict(data).get("data"),
        lambda c: _join(c.get("columnKey"), c.get("displayName"), c.get("category"), c.get("type")),
        f"Columns available for {k} tables",
    )


@tool
@_text_errors
def lusha_get_table(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
) -> str:
    """Get a table's details and processing status (is a column run still going?). Free."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    data, err = _request("GET", f"/v3/{k}/tables/{tid}", params=owner)
    if err:
        return err
    table = _as_dict(_as_dict(data).get("data"))
    if not table:
        return _dump(data, f"Table {tid}")
    lines = [f"Table {_table_line(table)}"]
    if table.get("workspaceUrl"):
        lines.append(f"Open in Lusha: {table['workspaceUrl']}")
    lines += [
        f"  column {_column_line(c)}" for c in _as_list(table.get("columns")) if isinstance(c, dict)
    ]
    return _clip("\n".join(lines))


@tool
@_text_errors
def lusha_update_table(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
    name: Annotated[str, "New name; empty keeps it"] = "",
    visibility: Annotated[str, "'private', 'shared' or empty to keep"] = "",
    archived: Annotated[str, "'true' archives, 'false' restores, empty keeps"] = "",
    new_owner_email: Annotated[str, "Hand the table to this user; empty keeps the owner"] = "",
) -> str:
    """Rename, share, archive or restore a table, or hand it to another user. Free."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(new_owner_email or owner_email)
    if err:
        return err
    body: dict[str, Any] = _clean(
        {"name": name.strip(), "visibility": visibility.strip().lower(), "owner": owner}
    )
    flag = archived.strip().lower()
    if flag in ("true", "false"):
        body["archived"] = flag == "true"
    elif flag:
        return "[tool-error] archived must be 'true', 'false' or empty."
    data, err = _request("PATCH", f"/v3/{k}/tables/{tid}", body=body)
    if err:
        return err
    return f"Updated {k} table {tid} ({', '.join(sorted(body))})."


@tool
@_text_errors
def lusha_delete_table(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
) -> str:
    """Permanently delete a table and all its data. This cannot be undone."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    data, err = _request("DELETE", f"/v3/{k}/tables/{tid}", params=owner)
    if err:
        return err
    return f"Deleted {k} table {tid} ({_as_dict(data).get('status') or 'done'})."


@tool
@_text_errors
def lusha_get_table_rows(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
    page: Annotated[int, "0-based, up to 100"] = 0,
    page_size: int = 25,
) -> str:
    """Read a page of a table's rows with every column value. Charges the export action per row."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    params = {**owner, "page": _page(page, 100), "size": _size(page_size, 100, 25)}
    data, err = _request("GET", f"/v3/{k}/tables/{tid}/entities", params=params)
    if err:
        return err
    return _table_rows_out(data, f"Table {tid} rows")


@tool
@_text_errors
def lusha_add_table_rows(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
    ids: Annotated[str, "Comma-separated Lusha contact or company ids, up to 500"],
    company_ids: Annotated[
        str, "Contacts tables only: each contact's company id, in the same order as ids"
    ] = "",
) -> str:
    """Add contacts or companies to a table. Free for contacts; companies tables charge
    the company reveal per newly added company.
    """
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    entity_ids, err = _ids("ids", ids, cap=500)
    if err:
        return err
    body = _clean(
        {
            "entityIds": entity_ids,
            "owner": owner,
            "companyIds": _csv(company_ids) if k == "contacts" else [],
        }
    )
    data, err = _request("POST", f"/v3/{k}/tables/{tid}/entities", body=body)
    if err:
        return err
    return _with_billing(_dump(_as_dict(data).get("data") or data, f"Added to table {tid}"), data)


@tool
@_text_errors
def lusha_remove_table_rows(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
    ids: Annotated[str, "Comma-separated ids of the rows to remove"],
) -> str:
    """Remove contacts or companies from a table. Free."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    entity_ids, err = _ids("ids", ids, cap=500)
    if err:
        return err
    data, err = _request(
        "DELETE", f"/v3/{k}/tables/{tid}/entities", body={"entityIds": entity_ids, "owner": owner}
    )
    if err:
        return err
    return _dump(_as_dict(data).get("data") or data, f"Removed from table {tid}")


@tool
@_text_errors
def lusha_list_table_columns(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
) -> str:
    """List a table's columns with their ids and how many cells are filled, pending or failed. Free."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    data, err = _request("GET", f"/v3/{k}/tables/{tid}/columns", params=owner)
    if err:
        return err
    return _rows(_as_dict(data).get("data"), _column_line, f"Columns of table {tid}")


@tool
@_text_errors
def lusha_add_table_columns(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    owner_email: Annotated[str, _OWNER_HELP],
    column_keys: Annotated[
        str, "Comma-separated column keys from lusha_get_table_column_catalog, up to 10"
    ],
) -> str:
    """Add Lusha data columns to a table; Lusha starts filling them on every row at once.

    Already-present columns come back with their existing id, so a re-run is
    safe. Adding is free today; the enrichment it starts may spend credits.
    """
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    keys, err = _ids("column_keys", column_keys, cap=10)
    if err:
        return err
    body = {"columns": [{"columnKey": c} for c in keys], "owner": owner}
    data, err = _request("POST", f"/v3/{k}/tables/{tid}/columns", body=body)
    if err:
        return err
    return _with_billing(
        _dump(_as_dict(data).get("data") or data, f"Columns added to table {tid}"), data
    )


@tool
@_text_errors
def lusha_remove_table_column(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    column_id: Annotated[str, "Column id from lusha_list_table_columns"],
    owner_email: Annotated[str, _OWNER_HELP],
) -> str:
    """Remove a column from a table and delete all of its cell data. Cannot be undone."""
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    cid, err = _seg("column_id", column_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    data, err = _request("DELETE", f"/v3/{k}/tables/{tid}/columns/{cid}", params=owner)
    if err:
        return err
    return f"Removed column {cid} from {k} table {tid}."


@tool
@_text_errors
def lusha_run_table_column(
    kind: Annotated[str, "'contacts' or 'companies'"],
    table_id: str,
    column_id: Annotated[str, "Column id from lusha_list_table_columns"],
    owner_email: Annotated[str, _OWNER_HELP],
    run_scope: Annotated[
        str,
        "'missing' (rows never run; cheapest), 'all' (every row again; charges again) or "
        "'specific' (only the ids given)",
    ] = "missing",
    ids: Annotated[str, "Comma-separated row ids; required for run_scope='specific'"] = "",
) -> str:
    """Fill or refresh one column of a table. Runs in the background and charges per
    row processed; lusha_get_table shows when it is done, lusha_get_table_rows the values.
    """
    k, err = _kind(kind)
    if err:
        return err
    tid, err = _seg("table_id", table_id)
    if err:
        return err
    cid, err = _seg("column_id", column_id)
    if err:
        return err
    owner, err = _owner(owner_email)
    if err:
        return err
    scope = run_scope.strip().lower()
    if scope not in ("missing", "all", "specific"):
        return "[tool-error] run_scope must be 'missing', 'all' or 'specific'."
    if scope == "specific" and not _csv(ids):
        return "[tool-error] run_scope='specific' needs the row ids."
    body = _clean(
        {"runScope": scope, "entityIds": _csv(ids) if scope == "specific" else [], "owner": owner}
    )
    data, err = _request("POST", f"/v3/{k}/tables/{tid}/columns/{cid}/run", body=body)
    if err:
        return err
    return _with_billing(
        f"Started column {cid} on {k} table {tid} ({scope} rows). Check progress with "
        f"lusha_get_table.\n{_compact(_as_dict(data).get('data') or {}, 600)}",
        data,
    )


# ── Webhooks ────────────────────────────────────────────────────────────────


def _subscription_line(s: dict[str, Any]) -> str:
    return _join(
        s.get("name"),
        s.get("entityType"),
        f"entity={s.get('entityId')}" if s.get("entityId") else "",
        ", ".join(map(str, s.get("signalTypes") or [])),
        s.get("url"),
        "active" if s.get("isActive") else "inactive",
        f"blocked: {s.get('blockReason')}" if s.get("blockReason") else "",
        f"id={s.get('id', '?')}",
    )


def _batch_out(data: Any, verb: str) -> str:
    data = _as_dict(data)
    head = f"{verb}: {data.get('successful', '?')} of {data.get('total', '?')} succeeded"
    failed = data.get("failed")
    if failed:
        head += f", {failed} failed"
    lines = [head + "."]
    for r in _as_list(data.get("results")):
        if isinstance(r, dict):
            lines.append(f"  {_compact(r, 300)}")
    return _clip("\n".join(lines))


@tool
@_text_errors
def lusha_create_webhook_subscriptions(
    url: Annotated[str, "HTTPS endpoint Lusha will POST signal events to"],
    entity_ids: Annotated[str, "Comma-separated Lusha contact or company ids to watch, up to 25"],
    entity_type: Annotated[str, "'contact' or 'company'"] = "contact",
    signal_types: Annotated[
        str, "Comma-separated signal types to deliver, e.g. 'promotion, companyChange'"
    ] = "",
    name: Annotated[str, "Name prefix for the subscriptions"] = "",
) -> str:
    """Subscribe to real-time signal events for contacts or companies, delivered to a URL.

    Lusha sends each event as a signed POST to the URL (verify it with the
    account secret from lusha_get_webhook_secret); a subscription that keeps
    failing is disabled after three retries.
    """
    etype = entity_type.strip().lower()
    if etype not in ("contact", "company"):
        return "[tool-error] entity_type must be 'contact' or 'company'."
    if not url.strip().lower().startswith("https://"):
        return "[tool-error] url must be an https:// address."
    ids, err = _ids("entity_ids", entity_ids, cap=25)
    if err:
        return err
    body = _clean(
        {
            "defaults": _clean(
                {"url": url.strip(), "entityType": etype, "signalTypes": _csv(signal_types)}
            ),
            "name": name.strip(),
            "subscriptions": [{"entityId": i} for i in ids],
        }
    )
    data, err = _request("POST", "/api/subscriptions", body=body)
    if err:
        return err
    return _batch_out(data, "Created subscriptions")


@tool
@_text_errors
def lusha_list_webhook_subscriptions(limit: Annotated[int, "1-100"] = 25, offset: int = 0) -> str:
    """List the account's webhook subscriptions, newest first."""
    params = {"limit": _size(limit, 100, 25), "offset": max(int(offset or 0), 0)}
    data, err = _request("GET", "/api/subscriptions", params=params)
    if err:
        return err
    pg = _as_dict(_as_dict(data).get("pagination"))
    out = _rows(_as_dict(data).get("data"), _subscription_line, "Webhook subscriptions")
    if pg.get("total") is not None:
        out += f"\n{pg['total']} in total."
    return out


@tool
@_text_errors
def lusha_get_webhook_subscription(subscription_id: str) -> str:
    """Get one webhook subscription, including why it was blocked, if it was."""
    sid, err = _seg("subscription_id", subscription_id)
    if err:
        return err
    data, err = _request("GET", f"/api/subscriptions/{sid}")
    if err:
        return err
    return _dump(data, f"Subscription {sid}")


@tool
@_text_errors
def lusha_update_webhook_subscription(
    subscription_id: str,
    url: Annotated[str, "New https:// URL; empty keeps it"] = "",
    signal_types: Annotated[str, "Comma-separated new signal types; empty keeps them"] = "",
    name: str = "",
    active: Annotated[
        str, "'true' (re)activates and clears a block, 'false' pauses, empty keeps"
    ] = "",
) -> str:
    """Change a webhook subscription's URL, signal types, name or active state."""
    sid, err = _seg("subscription_id", subscription_id)
    if err:
        return err
    if url.strip() and not url.strip().lower().startswith("https://"):
        return "[tool-error] url must be an https:// address."
    body: dict[str, Any] = _clean(
        {"url": url.strip(), "signalTypes": _csv(signal_types), "name": name.strip()}
    )
    flag = active.strip().lower()
    if flag in ("true", "false"):
        body["isActive"] = flag == "true"
    elif flag:
        return "[tool-error] active must be 'true', 'false' or empty."
    if not body:
        return "[tool-error] Nothing to change: give a url, signal_types, name or active."
    data, err = _request("PATCH", f"/api/subscriptions/{sid}", body=body)
    if err:
        return err
    return f"Updated subscription {sid} ({', '.join(sorted(body))})."


@tool
@_text_errors
def lusha_test_webhook_subscription(
    subscription_id: str,
    mode: Annotated[
        str, "'direct' (only checks the URL answers), 'kafka' or 'full' (end to end; default)"
    ] = "full",
) -> str:
    """Send a test event to a webhook subscription. Uses mock data and no credits."""
    sid, err = _seg("subscription_id", subscription_id)
    if err:
        return err
    m = mode.strip().lower()
    if m not in ("direct", "kafka", "full"):
        return "[tool-error] mode must be 'direct', 'kafka' or 'full'."
    data, err = _request("POST", f"/api/subscriptions/{sid}/test", params={"mode": m})
    if err:
        return err
    data = _as_dict(data)
    verdict = "succeeded" if data.get("isSuccess") else "failed"
    return _dump({k: v for k, v in data.items() if k != "testPayload"}, f"Test {verdict} for {sid}")


@tool
@_text_errors
def lusha_delete_webhook_subscriptions(
    subscription_ids: Annotated[str, "Comma-separated subscription ids, up to 25"],
) -> str:
    """Permanently delete webhook subscriptions."""
    ids, err = _ids("subscription_ids", subscription_ids, cap=25)
    if err:
        return err
    data, err = _request("POST", "/api/subscriptions/delete", body={"ids": ids})
    if err:
        return err
    return _batch_out(data, "Deleted subscriptions")


@tool
@_text_errors
def lusha_create_opt_out_subscription(
    url: Annotated[str, "HTTPS endpoint Lusha will POST opt-out notices to"],
    name: str = "",
) -> str:
    """Get notified when a contact opts out of Lusha's data processing, so the same
    emails and phones can be removed from the user's CRM and outreach tools.
    One per account.
    """
    if not url.strip().lower().startswith("https://"):
        return "[tool-error] url must be an https:// address."
    body = _clean({"entityType": "contact", "url": url.strip(), "name": name.strip()})
    data, err = _request("POST", "/api/subscriptions/opt-out", body=body)
    if err:
        return err
    return f"Created opt-out subscription: {_subscription_line(_as_dict(data))}."


@tool
@_text_errors
def lusha_get_webhook_delivery_logs(
    subscription_id: Annotated[str, "Only this subscription's deliveries"] = "",
    status: Annotated[str, "'success', 'failed', 'permanent_failure' or empty"] = "",
    limit: Annotated[int, "1-100"] = 25,
    offset: int = 0,
) -> str:
    """Read webhook delivery attempts: status codes, response times and errors."""
    st = status.strip().lower()
    if st and st not in ("success", "failed", "permanent_failure"):
        return "[tool-error] status must be 'success', 'failed' or 'permanent_failure'."
    params = _clean(
        {
            "subscriptionId": subscription_id.strip(),
            "status": st,
            "limit": _size(limit, 100, 25),
            "offset": max(int(offset or 0), 0) or None,
        }
    )
    data, err = _request("GET", "/api/audit-logs", params=params)
    if err:
        return err
    return _rows(
        _as_dict(data).get("data"),
        lambda r: _join(
            r.get("deliveredAt"),
            r.get("status"),
            f"HTTP {r['statusCode']}" if r.get("statusCode") else "",
            f"{r['durationMs']}ms" if r.get("durationMs") is not None else "",
            str(r.get("error") or "")[:200],
            f"subscription={r.get('subscriptionId')}" if r.get("subscriptionId") else "",
        ),
        "Webhook deliveries",
    )


@tool
@_text_errors
def lusha_get_webhook_delivery_stats(
    subscription_id: Annotated[str, "Only this subscription; empty for the whole account"] = "",
) -> str:
    """Webhook delivery totals: deliveries, successes, failures and success rate."""
    data, err = _request(
        "GET", "/api/audit-logs/stats", params=_clean({"subscriptionId": subscription_id.strip()})
    )
    if err:
        return err
    return _dump(data, "Webhook delivery statistics")


@tool
@_text_errors
def lusha_get_webhook_secret() -> str:
    """Read the account's webhook signing secret, used to verify that a delivery came
    from Lusha. Hand it only to the system that receives the webhooks.
    """
    data, err = _request("GET", "/api/account/secret")
    if err:
        return err
    secret = _as_dict(data).get("secret")
    return f"Webhook secret: {secret}" if secret else "The account has no webhook secret yet."


@tool
@_text_errors
def lusha_regenerate_webhook_secret() -> str:
    """Replace the account's webhook signing secret (creating one if there is none).

    The old secret stops working at once for every subscription, so every
    receiver must be updated with the new one, which is shown only here.
    """
    data, err = _request("POST", "/api/account/secret/regenerate")
    if err:
        return err
    return (
        f"New webhook secret: {_as_dict(data).get('secret')}. The previous one no longer "
        "verifies deliveries; update every webhook receiver."
    )


# ── Account ─────────────────────────────────────────────────────────────────


@tool
@_text_errors
def lusha_get_account_usage() -> str:
    """Show the Lusha account's credits (total, used, remaining), rate limits, plan and
    the credit price of every API action. Lusha allows 5 calls a minute to this endpoint.
    """
    data, err = _request("GET", "/v3/account/usage")
    if err:
        return err
    return _dump(data, "Lusha account usage", limit=6000)


# ── Generic escape hatch ────────────────────────────────────────────────────

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@tool
@_text_errors
def lusha_api_request(
    method: Annotated[str, "GET, POST, PUT, PATCH or DELETE"],
    path: Annotated[
        str,
        "Path on api.lusha.com, e.g. '/v2/person' or '/v3/contacts/search' (leading slash, no host)",
    ],
    body_json: Annotated[str, "Optional JSON request body"] = "",
    query_json: Annotated[str, "Optional JSON object of query parameters"] = "",
) -> str:
    """Call any Lusha API endpoint without a dedicated tool, including the legacy v2
    endpoints (/v2/person, /v2/company, /bulk/company/v2, /prospecting/..., /api/signals/...).
    The API key is applied for you. Reference: https://docs.lusha.com/api-reference/overview
    """
    verb = method.strip().upper()
    if verb not in _METHODS:
        return f"[tool-error] method must be one of {', '.join(_METHODS)}."
    p = "/" + path.strip().lstrip("/")
    if "://" in path or ".." in p or "?" in p or "#" in p:
        return (
            "[tool-error] path must be a relative API path such as '/v3/contacts/search'; "
            "put query parameters in query_json."
        )
    body, err = _loads("body_json", body_json)
    if err:
        return err
    query, err = _json_object("query_json", query_json)
    if err:
        return err
    data, err = _request(verb, p, params=query or None, body=body)
    if err:
        return err
    text = json.dumps(data, indent=2, default=str)
    if len(text) > _LIST_LIMIT:
        text = text[:_LIST_LIMIT] + "\n... (truncated)"
    return f"{verb} {p} OK\n{text}"


get_tools, get_tool = make_toolkit_api(globals())
