"""Apollo.io toolkit for the ApolloAgent subagent.

Apollo publishes no Python SDK: its documented route for client code is the
REST API (``https://api.apollo.io/api/v1``) described by an OpenAPI spec at
``https://docs.apollo.io/openapi/apollo-rest-api.json``. This toolkit is a thin
``httpx`` wrapper over that API, so it needs no extra package. One credential
drives it: ``APOLLO_API_KEY``, sent as the ``x-api-key`` header.

Coverage follows the API's own sections:

  • Prospecting: people search (free), people and organization enrichment,
    organization search, job postings, news
  • Records: contacts, accounts, deals, stages, lists, notes
  • Engagement: sequences (search, enroll, remove/stop), email accounts,
    tasks, outreach emails
  • Workspace: the acting user and credit balance, users, API usage
  • ``apollo_api_request`` for any endpoint without a dedicated tool

Two things about Apollo shape every tool here. **Credits**: many prospecting
endpoints spend the team's Apollo credits, so each tool's description says
what it costs and the free people search is the one to start from. **Key
scopes**: an Apollo key is either a master key or one limited to the endpoints
picked when it was created, so a 403 usually means "this key was not granted
this endpoint", and the error says so.
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

_BASE_URL = "https://api.apollo.io/api/v1"
_TIMEOUT = 30.0
_DETAIL_LIMIT = 4000

_CREDS_MSG = (
    "[missing-credentials] APOLLO_API_KEY not set. Ask the user for an Apollo API "
    'key with magic_link_request_secret("APOLLO_API_KEY") (Apollo: Settings, '
    "Integrations, API keys, Create new key; a master key unlocks every endpoint)."
)
_REFUSED_HINT = (
    " Apollo refused the API key: it was revoked, mistyped or belongs to another "
    'workspace. Ask the user for a fresh one with magic_link_request_secret("APOLLO_API_KEY").'
)
_SCOPE_HINT = (
    " This Apollo key is not allowed to call this endpoint. In Apollo (Settings, "
    "Integrations, API keys) edit the key and enable this endpoint, or create a "
    'master key, then store it with magic_link_request_secret("APOLLO_API_KEY").'
)
_RATE_HINT = (
    " Apollo's rate limit for this endpoint is used up; wait before retrying "
    "(apollo_get_api_usage shows the per-minute, hourly and daily limits)."
)
_CREDIT_HINT = (
    " The Apollo team may be out of credits for this action; apollo_whoami shows "
    "the remaining balance."
)


# ── HTTP ────────────────────────────────────────────────────────────────────


def _clean(mapping: dict[str, Any]) -> dict[str, Any]:
    """Drop unset values so Apollo applies its own defaults."""
    return {k: v for k, v in mapping.items() if v not in (None, "", [], {})}


def _csv(value: str) -> list[str]:
    """Split a comma-separated (or newline-separated) argument into a list."""
    return [p.strip() for p in str(value or "").replace("\n", ",").split(",") if p.strip()]


def _per_page(value: int, cap: int = 100) -> int:
    return min(max(int(value or 1), 1), cap)


def _error_text(resp: httpx.Response) -> str:
    """Apollo's own words for a failed call, as short as they stay readable."""
    try:
        data = resp.json()
    except ValueError:
        return resp.text.strip()[:400]
    if isinstance(data, dict):
        for key in ("error", "message", "errors", "error_message", "detail"):
            if data.get(key):
                val = data[key]
                return (val if isinstance(val, str) else json.dumps(val))[:400]
    return json.dumps(data)[:400]


# An asynchronous enrichment that has not finished answers 404 with a
# RESULT_PENDING code. That is a status, not a failure: reported as an error the
# agent reads "HTTP 404" and gives up on a lookup that is still running.
_PENDING_KEY = "_olano_result_pending"


def _pending_retry_after(resp: httpx.Response) -> int | None:
    """Seconds Apollo asks to wait when the result is still processing, else None."""
    if resp.status_code != 404:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("error_details")
    details: dict[str, Any] = raw if isinstance(raw, dict) else {}
    code = str(details.get("code") or data.get("error_code") or "").upper()
    if "RESULT_PENDING" not in code:
        return None
    # The top-level retry_after_seconds is deprecated; the suggestion carries it too.
    wait: Any = None
    for hint in details.get("suggestions") or []:
        if isinstance(hint, dict) and hint.get("retry_after_seconds") is not None:
            wait = hint["retry_after_seconds"]
            break
    if wait is None:
        wait = data.get("retry_after_seconds")
    try:
        return max(int(wait), 1)
    except (TypeError, ValueError):
        return 10


def _request(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | list[Any] | None = None,
) -> tuple[Any, str | None]:
    """Call the Apollo API. Returns ``(json, None)`` or ``(None, error_text)``."""
    key = get_credential("APOLLO_API_KEY")
    if not key:
        return None, _CREDS_MSG
    method = method.upper()
    where = f"Apollo {method} {path}"
    try:
        resp = httpx.request(
            method,
            f"{_BASE_URL}{path}",
            headers={
                "x-api-key": key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Cache-Control": "no-cache",
            },
            params=params or None,
            json=body if body is not None else None,
            timeout=_TIMEOUT,
        )
    except httpx.TimeoutException as exc:
        return None, (
            f"[tool-error] {where}: Apollo did not answer within {_TIMEOUT:.0f}s "
            f"({type(exc).__name__}). Retry once; for a search, ask for fewer results "
            "(smaller per_page)."
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
    pending = _pending_retry_after(resp)
    if pending is not None:
        return {_PENDING_KEY: pending}, None
    detail = _error_text(resp)
    msg = f"[tool-error] {where}: HTTP {resp.status_code}: {detail}"
    if resp.status_code == 401:
        msg += _REFUSED_HINT
    elif resp.status_code == 403:
        msg += _SCOPE_HINT
    elif resp.status_code == 429:
        msg += _RATE_HINT
    elif resp.status_code == 422 and "credit" in detail.lower():
        msg += _CREDIT_HINT
    return None, msg


# Apollo ids are hex strings; enrichment request ids are signed integers. Nothing
# else may reach a URL path: httpx resolves "..", so an id such as
# "../emailer_messages/1/send_now?x=" would turn an ungated call (completing a
# task) into a different endpoint (sending an email) behind the approval gate.
_ID_RE = re.compile(r"^-?[A-Za-z0-9_]+$")


def _seg(label: str, value: str) -> tuple[str, str | None]:
    """Validate one id for use as a URL path segment. Returns ``(id, error)``."""
    ident = str(value or "").strip()
    if not ident:
        return "", f"[tool-error] {label} is required; look it up with a search tool first."
    if not _ID_RE.match(ident):
        return "", (
            f"[tool-error] {label} {ident[:60]!r} is not an Apollo id (letters, digits and "
            "underscores only). Pass the id field from a search or get result."
        )
    return ident, None


def _loads(label: str, raw: str) -> tuple[Any, str | None]:
    if not str(raw or "").strip():
        return {}, None
    try:
        return json.loads(raw), None
    except json.JSONDecodeError as exc:
        return None, f"[tool-error] {label} is not valid JSON: {exc}"


# ── Formatting ──────────────────────────────────────────────────────────────


def _pages(data: Any) -> str:
    """'page 1/12, 230 total' from either pagination shape Apollo returns."""
    if not isinstance(data, dict):
        return ""
    pg = data.get("pagination") or {}
    total = pg.get("total_entries", data.get("total_entries"))
    page, pages = pg.get("page"), pg.get("total_pages")
    parts = []
    if page and pages:
        parts.append(f"page {page}/{pages}")
    if total is not None:
        parts.append(f"{total} total")
    return f" ({', '.join(parts)})" if parts else ""


def _join(*parts: Any, sep: str = " | ") -> str:
    return sep.join(str(p) for p in parts if p not in (None, "", [], {}))


def _person_name(p: dict[str, Any]) -> str:
    if p.get("name"):
        return str(p["name"])
    last = p.get("last_name") or p.get("last_name_obfuscated")
    return _join(p.get("first_name"), last, sep=" ") or "(no name)"


def _location(o: dict[str, Any]) -> str:
    return _join(o.get("city"), o.get("state"), o.get("country"), sep=", ")


def _person_line(p: dict[str, Any]) -> str:
    org = p.get("organization") or {}
    org_name = org.get("name") or p.get("organization_name")
    title = p.get("title")
    role = f"{title} @ {org_name}" if title and org_name else (title or org_name)
    flags = []
    if p.get("email"):
        flags.append(f"<{p['email']}>")
    elif p.get("has_email") is True:
        flags.append("email on file (enrich to reveal)")
    return _join(_person_name(p), role, _location(p), *flags, f"id={p.get('id', '?')}")


def _org_line(o: dict[str, Any]) -> str:
    domain = o.get("primary_domain") or o.get("domain") or o.get("website_url")
    size = o.get("estimated_num_employees")
    return _join(
        o.get("name") or "(no name)",
        domain,
        o.get("industry"),
        f"{size} employees" if size else "",
        _location(o),
        f"id={o.get('id', '?')}",
    )


def _contact_line(c: dict[str, Any]) -> str:
    role = _join(c.get("title"), c.get("organization_name"), sep=" @ ")
    return _join(
        _person_name(c),
        role,
        f"<{c['email']}>" if c.get("email") else "",
        c.get("email_status"),
        f"id={c.get('id', '?')}",
    )


def _account_line(a: dict[str, Any]) -> str:
    return _join(
        a.get("name") or "(no name)", a.get("domain"), a.get("phone"), f"id={a.get('id', '?')}"
    )


def _deal_line(d: dict[str, Any]) -> str:
    amount = d.get("amount")
    return _join(
        d.get("name") or "(no name)",
        f"amount={amount}" if amount not in (None, "") else "",
        f"stage={d.get('opportunity_stage_id')}" if d.get("opportunity_stage_id") else "",
        f"close={d.get('closed_date')}" if d.get("closed_date") else "",
        "closed" if d.get("is_closed") else "",
        f"id={d.get('id', '?')}",
    )


def _rows(items: list[Any], line: Any, heading: str, data: Any = None, empty: str = "") -> str:
    if not items:
        return empty or f"No {heading.lower()} found."
    out = [f"{heading}{_pages(data)}:"]
    out += [f"  {line(i)}" for i in items if isinstance(i, dict)]
    return "\n".join(out)


def _dump(obj: Any, label: str) -> str:
    """Non-empty fields of one record, as indented JSON clipped to a sane size."""
    if isinstance(obj, dict):
        obj = {k: v for k, v in obj.items() if v not in (None, "", [], {})}
    text = json.dumps(obj, indent=2, default=str)
    if len(text) > _DETAIL_LIMIT:
        text = text[:_DETAIL_LIMIT] + "\n... (truncated)"
    return f"{label}:\n{text}"


_PERSON_KEYS = (
    "id",
    "name",
    "title",
    "headline",
    "seniority",
    "email",
    "email_status",
    "personal_emails",
    "phone_numbers",
    "linkedin_url",
    "twitter_url",
    "city",
    "state",
    "country",
    "departments",
    "match_confidence",
    "contact_id",
)
_ORG_KEYS = (
    "id",
    "name",
    "primary_domain",
    "website_url",
    "linkedin_url",
    "industry",
    "keywords",
    "estimated_num_employees",
    "annual_revenue_printed",
    "total_funding_printed",
    "latest_funding_stage",
    "latest_funding_round_date",
    "founded_year",
    "phone",
    "raw_address",
    "short_description",
    "technology_names",
    "publicly_traded_symbol",
)


def _person_detail(p: dict[str, Any]) -> str:
    view: dict[str, Any] = {k: p.get(k) for k in _PERSON_KEYS}
    org = p.get("organization") or {}
    if org:
        view["organization"] = _join(org.get("name"), org.get("primary_domain"), org.get("id"))
    history = [
        _join(h.get("title"), h.get("organization_name"), h.get("start_date"), h.get("end_date"))
        for h in (p.get("employment_history") or [])[:5]
        if isinstance(h, dict)
    ]
    if history:
        view["employment_history"] = history
    return _dump(view, f"Person {_person_name(p)}")


def _org_detail(o: dict[str, Any]) -> str:
    view: dict[str, Any] = {k: o.get(k) for k in _ORG_KEYS}
    for key, cap in (("keywords", 15), ("technology_names", 25)):
        if isinstance(view.get(key), list):
            view[key] = view[key][:cap]
    return _dump(view, f"Organization {o.get('name') or '?'}")


# ── Errors are results ──────────────────────────────────────────────────────


def _text_errors(fn: Callable[..., str]) -> Callable[..., str]:
    """Make a tool return its failures as text the agent can act on, never raise.

    ``_request`` already turns every HTTP and transport failure into a
    ``[tool-error]`` string; this catches the rest (an Apollo response in a
    shape the formatter did not expect, a malformed argument) so the agent
    always gets a message that says what happened and what to do next.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # deliberate catch-all: errors go back as text
            logger.warning("%s failed: %s", fn.__name__, exc, exc_info=True)
            return (
                f"[tool-error] {fn.__name__}: {type(exc).__name__}: {str(exc)[:300]}. "
                "Apollo may already have applied a write before this failed, so check "
                "with a search or get call before retrying one; for a read, retry "
                "with simpler arguments or use apollo_api_request to see the raw answer."
            )

    return wrapper


# ── Prospecting: people ─────────────────────────────────────────────────────


@tool
@_text_errors
def apollo_search_people(
    titles: Annotated[str, "Comma-separated job titles, e.g. 'VP Sales, Head of Revenue'"] = "",
    keywords: Annotated[str, "Free-text keywords"] = "",
    person_name: Annotated[str, "Search by name only, e.g. 'Jordan Blake'"] = "",
    locations: Annotated[
        str, "Comma-separated places the people live: cities, states, countries"
    ] = "",
    seniorities: Annotated[
        str,
        "Comma-separated: owner, founder, c_suite, partner, vp, head, director, "
        "manager, senior, entry, intern",
    ] = "",
    company_domains: Annotated[str, "Comma-separated employer domains, e.g. 'stripe.com'"] = "",
    company_locations: Annotated[str, "Comma-separated employer HQ locations"] = "",
    employee_ranges: Annotated[
        str, "Comma-separated headcount ranges 'min,max' joined by ';', e.g. '1,50;51,200'"
    ] = "",
    industries: Annotated[str, "Comma-separated Apollo industry names (lowercase)"] = "",
    organization_ids: Annotated[str, "Comma-separated Apollo organization ids"] = "",
    email_status: Annotated[
        str, "Comma-separated: verified, unverified, likely to engage, unavailable"
    ] = "",
    exact_titles: Annotated[
        bool, "Match titles strictly instead of including similar ones"
    ] = False,
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Find net-new prospects in Apollo's people database. Costs 0 credits.

    Returns names, titles, employers and Apollo person ids, but never emails or
    phone numbers: pass a person id (or name + domain) to apollo_enrich_person
    to reveal contact details. Start prospecting here; it is the free search.
    """
    body = _clean(
        {
            "person_titles": _csv(titles),
            "include_similar_titles": False if exact_titles else None,
            "q_keywords": keywords.strip(),
            "q_person_name": person_name.strip(),
            "person_locations": _csv(locations),
            "person_seniorities": _csv(seniorities),
            "q_organization_domains_list": _csv(company_domains),
            "organization_locations": _csv(company_locations),
            "organization_num_employees_ranges": [
                r.strip() for r in employee_ranges.split(";") if r.strip()
            ],
            "organization_industries": _csv(industries),
            "organization_ids": _csv(organization_ids),
            "contact_email_status": _csv(email_status),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("POST", "/mixed_people/api_search", body=body)
    if err:
        return err
    return _rows(data.get("people") or [], _person_line, "People", data)


@tool
@_text_errors
def apollo_enrich_person(
    person_id: Annotated[str, "Apollo person id from apollo_search_people (best match)"] = "",
    email: str = "",
    linkedin_url: str = "",
    name: Annotated[str, "Full name; combine with domain or organization_name"] = "",
    first_name: str = "",
    last_name: str = "",
    domain: Annotated[str, "Employer domain, e.g. 'apollo.io'"] = "",
    organization_name: str = "",
    reveal_personal_emails: Annotated[bool, "Also return personal emails (more credits)"] = False,
    reveal_phone_number: Annotated[
        bool,
        "Request phone numbers. Apollo delivers them asynchronously: the result "
        "carries a request_id to read later with apollo_get_enrichment_result.",
    ] = False,
) -> str:
    """Enrich one person: work email, title, location, employment history.

    Costs 1 credit when data is found (personal emails and phones cost more;
    a mobile number is 8 credits). Identify the person by person_id, email or
    linkedin_url, or by name plus domain/organization_name. If the person is
    already a saved contact, the result names its contact_id.
    """
    params = _clean(
        {
            "id": person_id.strip(),
            "email": email.strip(),
            "linkedin_url": linkedin_url.strip(),
            "name": name.strip(),
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "domain": domain.strip(),
            "organization_name": organization_name.strip(),
            "reveal_personal_emails": True if reveal_personal_emails else None,
            "reveal_phone_number": True if reveal_phone_number else None,
            "poll_only": True if reveal_phone_number else None,
        }
    )
    if not any(k in params for k in ("id", "email", "linkedin_url", "name", "first_name")):
        return "[tool-error] apollo_enrich_person: give a person_id, email, linkedin_url or a name."
    data, err = _request("POST", "/people/match", body=params)
    if err:
        return err
    person = data.get("person") if isinstance(data, dict) else None
    if not person:
        return "Apollo found no matching person (no credits are charged for a miss)."
    out = _person_detail(person)
    if reveal_phone_number and data.get("request_id"):
        out += (
            f"\nPhone numbers are being looked up: request_id={data['request_id']}. "
            "Read them later with apollo_get_enrichment_result."
        )
    return out


@tool
@_text_errors
def apollo_bulk_enrich_people(
    people_json: Annotated[
        str,
        "JSON array of up to 10 objects, each with any of: id, email, linkedin_url, "
        'name, first_name, last_name, domain, organization_name. e.g. \'[{"email": "a@b.com"}]\'',
    ],
    reveal_personal_emails: bool = False,
) -> str:
    """Enrich up to 10 people in one call. Same credit cost per person as apollo_enrich_person."""
    details, err = _loads("people_json", people_json)
    if err:
        return err
    if not isinstance(details, list) or not details:
        return "[tool-error] people_json must be a non-empty JSON array."
    if len(details) > 10:
        return "[tool-error] Apollo enriches at most 10 people per call; split the list."
    body = _clean(
        {"details": details, "reveal_personal_emails": True if reveal_personal_emails else None}
    )
    data, err = _request("POST", "/people/bulk_match", body=body)
    if err:
        return err
    matches = [m for m in (data.get("matches") or []) if isinstance(m, dict)]
    head = f"Matched {len(matches)} of {len(details)}"
    if data.get("credits_consumed") is not None:
        head += f" ({data['credits_consumed']} credits)"
    lines = [head + ":"] + [f"  {_person_line(m)}" for m in matches]
    return "\n".join(lines)


@tool
@_text_errors
def apollo_get_enrichment_result(
    request_id: Annotated[str, "request_id returned by an enrichment that revealed phones"],
) -> str:
    """Read the asynchronous result of a phone (or waterfall) enrichment."""
    rid, err = _seg("request_id", request_id)
    if err:
        return err
    data, err = _request("GET", f"/webhook_result/{rid}")
    if err:
        return err
    if isinstance(data, dict) and _PENDING_KEY in data:
        return (
            f"Still processing (not an error): Apollo has not finished request_id={rid} yet. "
            f"Apollo suggests checking again in {data[_PENDING_KEY]}s; phone lookups often "
            "take several minutes. Check again later, or give the user the rest of the "
            "result now and say the phone numbers will follow."
        )
    return _dump(data, f"Enrichment result {request_id}")


# ── Prospecting: organizations ──────────────────────────────────────────────


@tool
@_text_errors
def apollo_search_organizations(
    name: Annotated[str, "Company name (partial match)"] = "",
    domains: Annotated[str, "Comma-separated domains"] = "",
    locations: Annotated[str, "Comma-separated HQ locations"] = "",
    employee_ranges: Annotated[
        str, "Headcount ranges 'min,max' joined by ';', e.g. '51,200;201,500'"
    ] = "",
    keyword_tags: Annotated[str, "Comma-separated company keywords, e.g. 'fintech, payments'"] = "",
    industries: Annotated[str, "Comma-separated Apollo industry names (lowercase)"] = "",
    technologies: Annotated[
        str, "Comma-separated technology uids used by the company, e.g. 'salesforce'"
    ] = "",
    revenue_min: int = 0,
    revenue_max: int = 0,
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Search Apollo's company database. Costs 1 credit per page of results.

    Returns company names, domains, industries, headcount and Apollo
    organization ids (feed those to apollo_search_people as organization_ids).
    """
    body = _clean(
        {
            "q_organization_name": name.strip(),
            "q_organization_domains_list": _csv(domains),
            "organization_locations": _csv(locations),
            "organization_num_employees_ranges": [
                r.strip() for r in employee_ranges.split(";") if r.strip()
            ],
            "q_organization_keyword_tags": _csv(keyword_tags),
            "organization_industries": _csv(industries),
            "currently_using_any_of_technology_uids": _csv(technologies),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    revenue = _clean({"min": revenue_min or None, "max": revenue_max or None})
    if revenue:
        body["revenue_range"] = revenue
    data, err = _request("POST", "/mixed_companies/search", body=body)
    if err:
        return err
    orgs = (data.get("organizations") or []) + (data.get("accounts") or [])
    return _rows(orgs, _org_line, "Organizations", data)


@tool
@_text_errors
def apollo_enrich_organization(
    domain: Annotated[str, "Company domain, e.g. 'apollo.io' (preferred)"] = "",
    name: str = "",
    linkedin_url: str = "",
    website: str = "",
) -> str:
    """Enrich one company: industry, headcount, revenue, funding, tech stack. Costs 1 credit."""
    params = _clean(
        {
            "domain": domain.strip(),
            "name": name.strip(),
            "linkedin_url": linkedin_url.strip(),
            "website": website.strip(),
        }
    )
    if not any(k in params for k in ("domain", "linkedin_url", "website")):
        return "[tool-error] apollo_enrich_organization: give a domain, linkedin_url or website."
    data, err = _request("GET", "/organizations/enrich", params=params)
    if err:
        return err
    org = data.get("organization") if isinstance(data, dict) else None
    if not org:
        return "Apollo found no matching organization."
    return _org_detail(org)


@tool
@_text_errors
def apollo_bulk_enrich_organizations(
    domains: Annotated[str, "Comma-separated company domains, up to 10"],
) -> str:
    """Enrich up to 10 companies by domain in one call. Costs 1 credit per company."""
    items = _csv(domains)
    if not items:
        return "[tool-error] Give at least one domain."
    if len(items) > 10:
        return "[tool-error] Apollo enriches at most 10 organizations per call; split the list."
    data, err = _request("POST", "/organizations/bulk_enrich", body={"domains": items})
    if err:
        return err
    return _rows(data.get("organizations") or [], _org_line, "Organizations", None)


@tool
@_text_errors
def apollo_get_organization_job_postings(
    organization_id: Annotated[str, "Apollo organization id"],
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List a company's active job postings (a hiring signal). Costs 1 credit per page."""
    oid, err = _seg("organization_id", organization_id)
    if err:
        return err
    data, err = _request(
        "GET",
        f"/organizations/{oid}/job_postings",
        params={"page": max(int(page or 1), 1), "per_page": _per_page(per_page, 10000)},
    )
    if err:
        return err
    postings = data.get("organization_job_postings") or data.get("job_postings") or []
    return _rows(
        postings,
        lambda j: _join(j.get("title"), _location(j), j.get("posted_at"), j.get("url")),
        "Job postings",
        data,
    )


@tool
@_text_errors
def apollo_search_news(
    organization_ids: Annotated[str, "Comma-separated Apollo organization ids"],
    categories: Annotated[str, "Comma-separated, e.g. 'hires, investment, contract'"] = "",
    published_after: Annotated[str, "YYYY-MM-DD"] = "",
    published_before: Annotated[str, "YYYY-MM-DD"] = "",
    page: int = 1,
    per_page: int = 10,
) -> str:
    """Search news articles about companies (funding, hires, launches). Costs 1 credit per page."""
    body = _clean(
        {
            "organization_ids": _csv(organization_ids),
            "categories": _csv(categories),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page, 25),
        }
    )
    published = _clean({"min": published_after.strip(), "max": published_before.strip()})
    if published:
        body["published_at"] = published
    data, err = _request("POST", "/news_articles/search", body=body)
    if err:
        return err
    return _rows(
        data.get("news_articles") or [],
        lambda a: _join(a.get("published_at"), a.get("title"), a.get("url")),
        "News",
        data,
    )


# ── Records: contacts ───────────────────────────────────────────────────────


@tool
@_text_errors
def apollo_search_contacts(
    keywords: Annotated[str, "Name, title, company or email keywords"] = "",
    stage_ids: Annotated[str, "Comma-separated contact stage ids (apollo_list_stages)"] = "",
    list_ids: Annotated[str, "Comma-separated list (label) ids (apollo_list_lists)"] = "",
    sort_by: Annotated[
        str,
        "contact_last_activity_date, contact_email_last_opened_at, "
        "contact_email_last_clicked_at, contact_created_at or contact_updated_at",
    ] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Search the contacts saved in the team's Apollo account. Costs 0 credits."""
    body = _clean(
        {
            "q_keywords": keywords.strip(),
            "contact_stage_ids": _csv(stage_ids),
            "contact_label_ids": _csv(list_ids),
            "sort_by_field": sort_by.strip(),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("POST", "/contacts/search", body=body)
    if err:
        return err
    return _rows(data.get("contacts") or [], _contact_line, "Contacts", data)


@tool
@_text_errors
def apollo_get_contact(contact_id: str) -> str:
    """Get one saved Apollo contact by id."""
    ident, err = _seg("contact_id", contact_id)
    if err:
        return err
    data, err = _request("GET", f"/contacts/{ident}")
    if err:
        return err
    return _dump(data.get("contact") or data, f"Contact {contact_id}")


@tool
@_text_errors
def apollo_create_contact(
    first_name: str,
    last_name: str,
    email: str = "",
    title: str = "",
    organization_name: str = "",
    account_id: str = "",
    website_url: str = "",
    phone: Annotated[str, "Direct phone number"] = "",
    list_names: Annotated[str, "Comma-separated list names to add the contact to"] = "",
    extra_json: Annotated[
        str,
        "Optional JSON of other contact fields, e.g. contact_stage_id, "
        "present_raw_address, mobile_phone, typed_custom_fields",
    ] = "",
    dedupe: Annotated[
        bool,
        "Match an existing contact (by email, then name plus company) and update it "
        "in place instead of creating a duplicate",
    ] = True,
) -> str:
    """Save a person as a contact in the team's Apollo account. Costs 0 credits.

    Apollo does not deduplicate contacts by default; this tool asks it to
    (dedupe=True) so re-running an import updates rather than duplicates. A
    match is updated in place: the fields sent here overwrite its stored ones
    and the id returned is the existing contact's. Matching falls back to name
    plus company, so pass dedupe=False when adding a different person who
    shares a name and employer with an existing contact.
    """
    extra, err = _loads("extra_json", extra_json)
    if err:
        return err
    if not isinstance(extra, dict):
        return "[tool-error] extra_json must be a JSON object of field names to values."
    body = _clean(
        {
            "first_name": first_name.strip(),
            "last_name": last_name.strip(),
            "email": email.strip(),
            "title": title.strip(),
            "organization_name": organization_name.strip(),
            "account_id": account_id.strip(),
            "website_url": website_url.strip(),
            "direct_phone": phone.strip(),
            "label_names": _csv(list_names),
            "run_dedupe": True if dedupe else None,
        }
    )
    body.update(extra)
    data, err = _request("POST", "/contacts", body=body)
    if err:
        return err
    contact = data.get("contact") or {}
    return f"Saved contact {_contact_line(contact) if contact else first_name + ' ' + last_name}."


@tool
@_text_errors
def apollo_update_contact(
    contact_id: str,
    fields_json: Annotated[
        str,
        'JSON of fields to change, e.g. \'{"title": "CTO", "contact_stage_id": "..."}\'. '
        "label_names REPLACES the contact's lists.",
    ],
) -> str:
    """Update a saved Apollo contact."""
    fields, err = _loads("fields_json", fields_json)
    if err:
        return err
    if not isinstance(fields, dict) or not fields:
        return "[tool-error] fields_json must be a non-empty JSON object."
    ident, err = _seg("contact_id", contact_id)
    if err:
        return err
    data, err = _request("PATCH", f"/contacts/{ident}", body=fields)
    if err:
        return err
    return f"Updated contact {contact_id} ({', '.join(sorted(fields))})."


# ── Records: accounts ───────────────────────────────────────────────────────


@tool
@_text_errors
def apollo_search_accounts(
    name: Annotated[str, "Part of the account (company) name"] = "",
    stage_ids: Annotated[str, "Comma-separated account stage ids"] = "",
    list_ids: Annotated[str, "Comma-separated list (label) ids"] = "",
    sort_by: Annotated[
        str, "account_last_activity_date, account_created_at or account_updated_at"
    ] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Search the accounts (companies) saved in the team's Apollo account. Costs 0 credits."""
    body = _clean(
        {
            "q_organization_name": name.strip(),
            "account_stage_ids": _csv(stage_ids),
            "account_label_ids": _csv(list_ids),
            "sort_by_field": sort_by.strip(),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("POST", "/accounts/search", body=body)
    if err:
        return err
    return _rows(data.get("accounts") or [], _account_line, "Accounts", data)


@tool
@_text_errors
def apollo_get_account(account_id: str) -> str:
    """Get one saved Apollo account by id."""
    ident, err = _seg("account_id", account_id)
    if err:
        return err
    data, err = _request("GET", f"/accounts/{ident}")
    if err:
        return err
    return _dump(data.get("account") or data, f"Account {account_id}")


@tool
@_text_errors
def apollo_create_account(
    name: str,
    domain: Annotated[str, "e.g. 'apollo.io' (no www.)"] = "",
    owner_id: Annotated[str, "Apollo user id (apollo_list_users)"] = "",
    stage_id: Annotated[str, "Account stage id (apollo_list_stages)"] = "",
    phone: str = "",
    raw_address: Annotated[str, "City, state, country"] = "",
    extra_json: Annotated[str, "Optional JSON of other fields, e.g. typed_custom_fields"] = "",
) -> str:
    """Save a company as an account in the team's Apollo account. Costs 0 credits."""
    extra, err = _loads("extra_json", extra_json)
    if err:
        return err
    if not isinstance(extra, dict):
        return "[tool-error] extra_json must be a JSON object of field names to values."
    body = _clean(
        {
            "name": name.strip(),
            "domain": domain.strip(),
            "owner_id": owner_id.strip(),
            "account_stage_id": stage_id.strip(),
            "phone": phone.strip(),
            "raw_address": raw_address.strip(),
        }
    )
    body.update(extra)
    data, err = _request("POST", "/accounts", body=body)
    if err:
        return err
    account = data.get("account") or {}
    return f"Saved account {_account_line(account) if account else name}."


@tool
@_text_errors
def apollo_update_account(
    account_id: str,
    fields_json: Annotated[str, 'JSON of fields to change, e.g. \'{"account_stage_id": "..."}\''],
) -> str:
    """Update a saved Apollo account."""
    fields, err = _loads("fields_json", fields_json)
    if err:
        return err
    if not isinstance(fields, dict) or not fields:
        return "[tool-error] fields_json must be a non-empty JSON object."
    ident, err = _seg("account_id", account_id)
    if err:
        return err
    data, err = _request("PATCH", f"/accounts/{ident}", body=fields)
    if err:
        return err
    return f"Updated account {account_id} ({', '.join(sorted(fields))})."


# ── Records: deals ──────────────────────────────────────────────────────────


@tool
@_text_errors
def apollo_list_deals(
    sort_by: Annotated[str, "amount, is_closed or is_won"] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List the deals (opportunities) in the team's Apollo account."""
    params = _clean(
        {
            "sort_by_field": sort_by.strip(),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("GET", "/opportunities/search", params=params)
    if err:
        return err
    return _rows(data.get("opportunities") or [], _deal_line, "Deals", data)


@tool
@_text_errors
def apollo_get_deal(deal_id: str) -> str:
    """Get one Apollo deal by id."""
    ident, err = _seg("deal_id", deal_id)
    if err:
        return err
    data, err = _request("GET", f"/opportunities/{ident}")
    if err:
        return err
    return _dump(data.get("opportunity") or data, f"Deal {deal_id}")


@tool
@_text_errors
def apollo_create_deal(
    name: str,
    account_id: Annotated[str, "Apollo account id of the target company"] = "",
    amount: Annotated[str, "Plain number, no commas or currency symbol"] = "",
    stage_id: Annotated[str, "Deal stage id (apollo_list_stages kind='deal')"] = "",
    owner_id: str = "",
    closed_date: Annotated[str, "Expected close date, YYYY-MM-DD"] = "",
    extra_json: Annotated[str, "Optional JSON of other fields, e.g. typed_custom_fields"] = "",
) -> str:
    """Create a deal in the team's Apollo account."""
    extra, err = _loads("extra_json", extra_json)
    if err:
        return err
    if not isinstance(extra, dict):
        return "[tool-error] extra_json must be a JSON object of field names to values."
    body = _clean(
        {
            "name": name.strip(),
            "account_id": account_id.strip(),
            "amount": amount.strip(),
            "opportunity_stage_id": stage_id.strip(),
            "owner_id": owner_id.strip(),
            "closed_date": closed_date.strip(),
        }
    )
    body.update(extra)
    data, err = _request("POST", "/opportunities", body=body)
    if err:
        return err
    deal = data.get("opportunity") or {}
    return f"Created deal {_deal_line(deal) if deal else name}."


@tool
@_text_errors
def apollo_update_deal(
    deal_id: str,
    fields_json: Annotated[
        str, 'JSON of fields to change, e.g. \'{"amount": "12000", "opportunity_stage_id": "..."}\''
    ],
) -> str:
    """Update an Apollo deal (stage, amount, close date, owner, name)."""
    fields, err = _loads("fields_json", fields_json)
    if err:
        return err
    if not isinstance(fields, dict) or not fields:
        return "[tool-error] fields_json must be a non-empty JSON object."
    ident, err = _seg("deal_id", deal_id)
    if err:
        return err
    data, err = _request("PATCH", f"/opportunities/{ident}", body=fields)
    if err:
        return err
    return f"Updated deal {deal_id} ({', '.join(sorted(fields))})."


# ── Records: stages, lists, notes ───────────────────────────────────────────

_STAGE_PATHS: dict[str, tuple[str, str]] = {
    "contact": ("/contact_stages", "contact_stages"),
    "account": ("/account_stages", "account_stages"),
    "deal": ("/opportunity_stages", "opportunity_stages"),
}
_STAGE_ALIASES: dict[str, str] = {
    "contact": "contact",
    "contacts": "contact",
    "account": "account",
    "accounts": "account",
    "deal": "deal",
    "deals": "deal",
    "opportunity": "deal",
    "opportunities": "deal",
}


@tool
@_text_errors
def apollo_list_stages(
    kind: Annotated[str, "'contact', 'account' or 'deal'"] = "contact",
) -> str:
    """List the stage ids and names for contacts, accounts or deals."""
    key = _STAGE_ALIASES.get(kind.strip().lower(), "")
    if key not in _STAGE_PATHS:
        return "[tool-error] kind must be 'contact', 'account' or 'deal'."
    path, field = _STAGE_PATHS[key]
    data, err = _request("GET", path)
    if err:
        return err
    stages = data.get(field) if isinstance(data, dict) else data
    return _rows(
        stages or [],
        lambda s: _join(s.get("name"), s.get("category"), f"id={s.get('id', '?')}"),
        f"{key.title()} stages",
    )


@tool
@_text_errors
def apollo_list_lists() -> str:
    """List the team's Apollo lists (labels) with their ids and record counts."""
    data, err = _request("GET", "/labels")
    if err:
        return err
    labels = data if isinstance(data, list) else (data.get("labels") or [])
    return _rows(
        labels,
        lambda lb: _join(
            lb.get("name"),
            lb.get("modality"),
            f"{lb['cached_count']} records" if lb.get("cached_count") is not None else "",
            f"id={lb.get('id', '?')}",
        ),
        "Lists",
    )


@tool
@_text_errors
def apollo_add_to_list(
    entity_ids: Annotated[str, "Comma-separated contact or account ids"],
    list_names: Annotated[str, "Comma-separated list names; a missing list is created"],
    modality: Annotated[str, "'contacts' or 'accounts'"] = "contacts",
) -> str:
    """Add saved contacts or accounts to one or more Apollo lists."""
    mod = modality.strip().lower()
    if mod not in ("contacts", "accounts"):
        return "[tool-error] modality must be 'contacts' or 'accounts'."
    ids, names = _csv(entity_ids), _csv(list_names)
    if not ids or not names:
        return "[tool-error] Give at least one entity id and one list name."
    body = {"entity_ids": ids, "label_names": names, "modality": mod}
    data, err = _request("POST", "/labels/add_entity_ids_to_label_names", body=body)
    if err:
        return err
    return f"Added {len(ids)} {mod} to {', '.join(names)}."


@tool
@_text_errors
def apollo_list_notes(
    contact_id: str = "",
    account_id: str = "",
    deal_id: str = "",
    limit: int = 25,
) -> str:
    """List notes attached to an Apollo contact, account or deal."""
    params = _clean(
        {
            "contact_id": contact_id.strip(),
            "account_id": account_id.strip(),
            "opportunity_id": deal_id.strip(),
            "limit": _per_page(limit),
        }
    )
    data, err = _request("GET", "/notes", params=params)
    if err:
        return err
    notes = data if isinstance(data, list) else (data.get("notes") or [])
    return _rows(
        notes,
        lambda n: _join(
            n.get("created_at"), str(n.get("content") or n.get("body") or "")[:200], n.get("id")
        ),
        "Notes",
    )


# ── Engagement: sequences ───────────────────────────────────────────────────


@tool
@_text_errors
def apollo_search_sequences(
    name: Annotated[str, "Part of the sequence name"] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Find the team's Apollo sequences (email cadences) and their ids."""
    body = _clean(
        {"q_name": name.strip(), "page": max(int(page or 1), 1), "per_page": _per_page(per_page)}
    )
    data, err = _request("POST", "/emailer_campaigns/search", body=body)
    if err:
        return err
    return _rows(
        data.get("emailer_campaigns") or [],
        lambda s: _join(
            s.get("name"),
            "active" if s.get("active") else "inactive",
            "archived" if s.get("archived") else "",
            f"{s['num_steps']} steps" if s.get("num_steps") else "",
            f"delivered={s.get('unique_delivered', 0)} replied={s.get('unique_replied', 0)}",
            f"id={s.get('id', '?')}",
        ),
        "Sequences",
        data,
    )


@tool
@_text_errors
def apollo_list_email_accounts() -> str:
    """List the mailboxes linked to Apollo, i.e. the senders a sequence can use."""
    data, err = _request("GET", "/email_accounts")
    if err:
        return err
    accounts = data.get("email_accounts") if isinstance(data, dict) else data
    return _rows(
        accounts or [],
        lambda a: _join(
            a.get("email"),
            "active" if a.get("active") else "inactive",
            "default" if a.get("default") else "",
            f"id={a.get('id', '?')}",
        ),
        "Email accounts",
    )


@tool
@_text_errors
def apollo_add_contacts_to_sequence(
    sequence_id: Annotated[str, "Sequence id from apollo_search_sequences"],
    contact_ids: Annotated[str, "Comma-separated saved contact ids (not person ids)"],
    send_from_email_account_id: Annotated[str, "Mailbox id from apollo_list_email_accounts"],
    include_unverified_emails: bool = False,
    include_active_in_other_sequences: bool = False,
    include_recent_job_changes: bool = False,
) -> str:
    """Enroll saved contacts in an Apollo sequence. This starts real emails to real people.

    Only contacts can be enrolled: save a prospect with apollo_create_contact
    first. By default Apollo skips contacts with unverified emails, contacts
    already in another sequence and recent job changers; the flags override that.
    """
    ids = _csv(contact_ids)
    if not ids:
        return "[tool-error] Give at least one contact id."
    sid, err = _seg("sequence_id", sequence_id)
    if err:
        return err
    body = _clean(
        {
            "emailer_campaign_id": sid,
            "contact_ids": ids,
            "send_email_from_email_account_id": send_from_email_account_id.strip(),
            "sequence_unverified_email": True if include_unverified_emails else None,
            "sequence_active_in_other_campaigns": True
            if include_active_in_other_sequences
            else None,
            "sequence_job_change": True if include_recent_job_changes else None,
        }
    )
    data, err = _request("POST", f"/emailer_campaigns/{sid}/add_contact_ids", body=body)
    if err:
        return err
    # `contacts` lists only the contacts Apollo actually added; the rest are in
    # `skipped_contact_ids` with a reason code each.
    added = [c for c in (data.get("contacts") or []) if isinstance(c, dict)]
    skipped = data.get("skipped_contact_ids") or {}
    out = f"Sequence {sid}: added {len(added)} of {len(ids)} contact(s)."
    if skipped:
        out += (
            f" Skipped by Apollo (contact id: reason): {json.dumps(skipped)[:600]}. "
            "The include_* flags override the eligibility skips."
        )
    elif not added:
        out += " Apollo reported no contacts added; check the ids with apollo_get_contact."
    return out


@tool
@_text_errors
def apollo_update_sequence_contacts(
    sequence_ids: Annotated[str, "Comma-separated sequence ids"],
    contact_ids: Annotated[str, "Comma-separated contact ids"],
    mode: Annotated[str, "'remove', 'stop' (halt their progress) or 'mark_as_finished'"],
) -> str:
    """Remove, stop or finish contacts in one or more Apollo sequences."""
    m = mode.strip().lower()
    if m not in ("remove", "stop", "mark_as_finished"):
        return "[tool-error] mode must be 'remove', 'stop' or 'mark_as_finished'."
    seqs, ids = _csv(sequence_ids), _csv(contact_ids)
    if not seqs or not ids:
        return "[tool-error] Give at least one sequence id and one contact id."
    body = {"emailer_campaign_ids": seqs, "contact_ids": ids, "mode": m}
    data, err = _request("POST", "/emailer_campaigns/remove_or_stop_contact_ids", body=body)
    if err:
        return err
    return f"Applied '{m}' to {len(ids)} contact(s) in {len(seqs)} sequence(s)."


@tool
@_text_errors
def apollo_search_outreach_emails(
    keywords: str = "",
    sequence_ids: Annotated[str, "Comma-separated sequence ids"] = "",
    statuses: Annotated[
        str, "Comma-separated: delivered, opened, clicked, bounced, unsubscribed, completed, ..."
    ] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """Search outreach emails sent through Apollo sequences, with their status."""
    params = _clean(
        {
            "q_keywords": keywords.strip(),
            "emailer_campaign_ids[]": _csv(sequence_ids),
            "emailer_message_stats[]": _csv(statuses),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("GET", "/emailer_messages/search", params=params)
    if err:
        return err
    return _rows(
        data.get("emailer_messages") or [],
        lambda e: _join(
            e.get("status"),
            e.get("to_email"),
            str(e.get("subject") or "")[:80],
            e.get("completed_at") or e.get("due_at"),
            f"id={e.get('id', '?')}",
        ),
        "Outreach emails",
        data,
    )


# ── Engagement: tasks ───────────────────────────────────────────────────────

_TASK_TYPES = (
    "call",
    "outreach_manual_email",
    "linkedin_step_connect",
    "linkedin_step_message",
    "linkedin_step_view_profile",
    "linkedin_step_interact_post",
    "action_item",
)


def _task_line(t: dict[str, Any]) -> str:
    return _join(
        t.get("type"),
        t.get("status"),
        t.get("priority"),
        t.get("due_at"),
        str(t.get("title") or t.get("note") or "")[:80],
        f"contact={t.get('contact_id')}" if t.get("contact_id") else "",
        f"id={t.get('id', '?')}",
    )


@tool
@_text_errors
def apollo_search_tasks(
    sort_by: Annotated[str, "task_due_at or task_priority"] = "",
    page: int = 1,
    per_page: int = 25,
) -> str:
    """List the team's Apollo tasks."""
    body = _clean(
        {
            "sort_by_field": sort_by.strip(),
            "page": max(int(page or 1), 1),
            "per_page": _per_page(per_page),
        }
    )
    data, err = _request("POST", "/tasks/search", body=body)
    if err:
        return err
    return _rows(data.get("tasks") or [], _task_line, "Tasks", data)


@tool
@_text_errors
def apollo_create_task(
    contact_id: str,
    user_id: Annotated[str, "Task owner's Apollo user id (apollo_whoami / apollo_list_users)"],
    due_at: Annotated[str, "ISO 8601 datetime, e.g. '2026-11-02T15:00:00Z' (GMT unless offset)"],
    task_type: Annotated[
        str,
        "call, outreach_manual_email, linkedin_step_connect, linkedin_step_message, "
        "linkedin_step_view_profile, linkedin_step_interact_post or action_item",
    ] = "action_item",
    priority: Annotated[str, "high, medium or low"] = "medium",
    title: str = "",
    note: str = "",
) -> str:
    """Create a scheduled Apollo task for a teammate to act on a contact."""
    if task_type not in _TASK_TYPES:
        return f"[tool-error] task_type must be one of: {', '.join(_TASK_TYPES)}."
    body = _clean(
        {
            "contact_id": contact_id.strip(),
            "user_id": user_id.strip(),
            "due_at": due_at.strip(),
            "type": task_type,
            "priority": priority.strip().lower(),
            "status": "scheduled",
            "title": title.strip(),
            "note": note.strip(),
        }
    )
    data, err = _request("POST", "/tasks", body=body)
    if err:
        return err
    task = data.get("task") or {}
    return f"Created task {_task_line(task) if task else task_type}."


@tool
@_text_errors
def apollo_complete_task(task_id: str, note: str = "") -> str:
    """Mark an Apollo task completed, optionally with a note."""
    tid, err = _seg("task_id", task_id)
    if err:
        return err
    data, err = _request("POST", f"/tasks/{tid}/complete", body=_clean({"note": note.strip()}))
    if err:
        return err
    return f"Completed task {task_id}."


# ── Workspace ───────────────────────────────────────────────────────────────


@tool
@_text_errors
def apollo_whoami(include_credit_usage: bool = True) -> str:
    """Show the Apollo user the API key acts as and the team's remaining credits.

    An Apollo API key acts as the workspace's longest-standing active admin,
    not as whoever created it. Use the returned id as a default owner/user id.
    """
    params = {"include_credit_usage": "true"} if include_credit_usage else None
    data, err = _request("GET", "/users/api_profile", params=params)
    if err:
        return err
    return _dump(data, "Apollo API profile")


@tool
@_text_errors
def apollo_list_users(page: int = 1, per_page: int = 50) -> str:
    """List the users on the Apollo team (owner ids for records and tasks). Needs a master key."""
    params = {"page": max(int(page or 1), 1), "per_page": _per_page(per_page)}
    data, err = _request("GET", "/users/search", params=params)
    if err:
        return err
    return _rows(
        data.get("users") or [],
        lambda u: _join(
            _join(u.get("first_name"), u.get("last_name"), sep=" ") or u.get("name"),
            u.get("email"),
            f"id={u.get('id', '?')}",
        ),
        "Users",
        data,
    )


@tool
@_text_errors
def apollo_get_api_usage() -> str:
    """Show Apollo API usage and rate limits per endpoint (minute, hour, day)."""
    data, err = _request("POST", "/usage_stats/api_usage_stats", body={})
    if err:
        return err
    return _dump(data, "Apollo API usage")


# ── Generic escape hatch ────────────────────────────────────────────────────

_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")


@tool
@_text_errors
def apollo_api_request(
    method: Annotated[str, "GET, POST, PUT, PATCH or DELETE"],
    path: Annotated[str, "Path under /api/v1, e.g. '/phone_calls/search' (leading slash, no host)"],
    body_json: Annotated[str, "Optional JSON request body"] = "",
    query_json: Annotated[str, "Optional JSON object of query parameters"] = "",
) -> str:
    """Call any Apollo REST endpoint without a dedicated tool (calls, conversations,
    custom fields, analytics reports, bulk endpoints, ...). The API key is applied
    for you. Reference: https://docs.apollo.io/reference/apollo-api
    """
    verb = method.strip().upper()
    if verb not in _METHODS:
        return f"[tool-error] method must be one of {', '.join(_METHODS)}."
    p = "/" + path.strip().lstrip("/")
    if p.startswith("/api/v1/"):
        p = p[len("/api/v1") :]
    if "://" in path or ".." in p:
        return "[tool-error] path must be a relative API path such as '/contacts/search'."
    body, err = _loads("body_json", body_json)
    if err:
        return err
    query, err = _loads("query_json", query_json)
    if err:
        return err
    if not isinstance(query, dict):
        return "[tool-error] query_json must be a JSON object of parameter names to values."
    data, err = _request(
        verb,
        p,
        params=query or None,
        body=body if body_json.strip() else None,
    )
    if err:
        return err
    if isinstance(data, dict) and _PENDING_KEY in data:
        return (
            f"{verb} {p}: still processing (not an error). Apollo suggests checking "
            f"again in {data[_PENDING_KEY]}s."
        )
    text = json.dumps(data, indent=2, default=str)
    if len(text) > _DETAIL_LIMIT:
        text = text[:_DETAIL_LIMIT] + "\n... (truncated)"
    return f"{verb} {p} OK\n{text}"


get_tools, get_tool = make_toolkit_api(globals())
