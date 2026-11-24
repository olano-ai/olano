"""SalesforceAgent tools — CRM via Salesforce REST API.

Auth env vars:
  SALESFORCE_INSTANCE_URL — e.g. https://yourorg.salesforce.com
  SALESFORCE_ACCESS_TOKEN — OAuth 2.0 access token

Both normally come from the Salesforce connector (dashboard Connections tab,
or ``magic_link_connect_service("salesforce")``), which mints and refreshes
them; a hand-set token in the vault works too.

Every request goes through :func:`_request`, and a ``401`` there is handled
rather than relayed. Salesforce drops sessions on its own schedule (an org's
session policy, an admin revoking a session, a password change) while the
stored record still looks valid, so the connector read "Connected" while every
tool returned ``401 Unauthorized`` and the agent could only report it. Now the
first 401 asks the broker for a refreshed token and retries once; a second 401
(or nothing to refresh) returns a ``[connector-auth]`` result that marks the
connection *Needs reconnecting* and tells the model to offer the reconnect
link, so the person fixes it from the chat instead of finding the dashboard.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, Any, cast

import httpx
from langchain_core.tools import tool

from olano.tools._connector_auth import (
    connector_auth_failure,
    recover_connector_credential,
)
from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_TOKEN_NAME = "SALESFORCE_ACCESS_TOKEN"
_API_VERSION = "v59.0"


class SalesforceAuthRefused(Exception):
    """The access token was refused and could not be recovered; ``str()`` is the tool result."""


def _base() -> str:
    url = get_credential("SALESFORCE_INSTANCE_URL", "").rstrip("/")
    if not url:
        raise ValueError(
            "SALESFORCE_INSTANCE_URL not set. Connect Salesforce through the dashboard's "
            "Connections tab or magic_link_connect_service('salesforce'); the connector "
            "captures the instance URL for you."
        )
    return f"{url}/services/data/{_API_VERSION}"


def _headers(token: str | None = None) -> dict:
    token = token or get_credential(_TOKEN_NAME, "")
    if not token:
        raise ValueError(
            "SALESFORCE_ACCESS_TOKEN not set. Connect Salesforce through the dashboard's "
            "Connections tab or magic_link_connect_service('salesforce')."
        )
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _error_text(r: httpx.Response) -> str:
    """Salesforce's own words for a refusal, when its JSON error body carries them.

    Salesforce answers ``[{"message": "Session expired or invalid", "errorCode":
    "INVALID_SESSION_ID"}]``; ``raise_for_status`` alone would report only
    ``Client error '401 Unauthorized' for url …`` and lose the reason.
    """
    reason = f"{r.status_code} {r.reason_phrase}".strip()
    try:
        body = r.json()
    except Exception:
        return reason
    if isinstance(body, list) and body and isinstance(body[0], dict):
        body = body[0]
    if isinstance(body, dict):
        message = str(body.get("message") or body.get("error_description") or "").strip()
        code = str(body.get("errorCode") or body.get("error") or "").strip()
        if message and code:
            return f"{reason}: {message} ({code})"
        if message or code:
            return f"{reason}: {message or code}"
    return reason


def _request(tool_name: str, method: str, path: str, **kwargs: Any) -> httpx.Response:
    """One Salesforce REST call, with the 401 handled instead of relayed.

    On a 401 the broker is asked to refresh the connector token and the call is
    retried once with the fresh token. A 401 on the retry, or nothing to refresh
    (a hand-set token), raises :class:`SalesforceAuthRefused` whose text is the
    model-facing ``[connector-auth]`` result. Every other status raises through
    :func:`_raise_for_status` as before, with Salesforce's error body attached.
    """
    timeout = kwargs.pop("timeout", 15)
    url = f"{_base()}{path}"
    r = httpx.request(method, url, headers=_headers(), timeout=timeout, **kwargs)
    if r.status_code == 401:
        refused = _error_text(r)
        recovery = recover_connector_credential(_TOKEN_NAME, error=refused)
        if recovery is not None and recovery.recovered:
            r = httpx.request(
                method, url, headers=_headers(recovery.value), timeout=timeout, **kwargs
            )
            if r.status_code != 401:
                return _raise_for_status(r)
            refused = _error_text(r)
        raise SalesforceAuthRefused(
            connector_auth_failure(_TOKEN_NAME, tool=tool_name, error=refused, recovery=recovery)
        )
    return _raise_for_status(r)


def _raise_for_status(r: httpx.Response) -> httpx.Response:
    if r.status_code >= 400:
        raise httpx.HTTPStatusError(_error_text(r), request=r.request, response=r)
    return r


def _tool_error(label: str, exc: Exception) -> str:
    if isinstance(exc, SalesforceAuthRefused):
        return str(exc)
    return f"[tool-error] {label}: {exc}"


@tool
def sf_query(
    soql: Annotated[str, "SOQL query (e.g. 'SELECT Id, Name FROM Account LIMIT 10')"],
) -> str:
    """Run a SOQL query against Salesforce and return results."""
    try:
        r = _request("sf_query", "GET", "/query", params={"q": soql}, timeout=20)
        data = r.json()
        records = data.get("records", [])
        total = data.get("totalSize", len(records))
        if not records:
            return "No records found."

        lines = [f"Salesforce query ({total} total, showing {len(records)}):"]
        for rec in records:
            attrs = {k: v for k, v in rec.items() if k != "attributes" and v is not None}
            lines.append("  " + " | ".join(f"{k}: {v}" for k, v in list(attrs.items())[:6]))
        return "\n".join(lines)
    except Exception as e:
        return _tool_error("salesforce", e)


@tool
def sf_get_record(
    object_type: Annotated[
        str, "Salesforce object type (e.g. Account, Contact, Lead, Opportunity)"
    ],
    record_id: Annotated[str, "Salesforce record ID"],
    fields: Annotated[str, "Comma-separated fields to return (optional)"] = "",
) -> str:
    """Get a specific Salesforce record by ID."""
    try:
        params = {"fields": fields} if fields else {}
        r = _request("sf_get_record", "GET", f"/sobjects/{object_type}/{record_id}", params=params)
        rec = r.json()
        lines = [f"Salesforce {object_type} ({record_id}):"]
        for k, v in rec.items():
            if k != "attributes" and v is not None:
                lines.append(f"  {k}: {v}")
        return "\n".join(lines)
    except Exception as e:
        return _tool_error("salesforce", e)


@tool
def sf_create_record(
    object_type: Annotated[str, "Salesforce object type (e.g. Lead, Contact, Account)"],
    fields_json: Annotated[str, "JSON string of field name→value pairs"],
) -> str:
    """Create a new Salesforce record."""
    try:
        fields = json.loads(fields_json)
        r = _request("sf_create_record", "POST", f"/sobjects/{object_type}", json=fields)
        result = r.json()
        return f"Created {object_type}: id={result.get('id')} success={result.get('success')}"
    except Exception as e:
        return _tool_error("sf_create_record", e)


@tool
def sf_update_record(
    object_type: Annotated[str, "Salesforce object type"],
    record_id: Annotated[str, "Record ID to update"],
    fields_json: Annotated[str, "JSON string of field name→value pairs to update"],
) -> str:
    """Update an existing Salesforce record."""
    try:
        fields = json.loads(fields_json)
        r = _request(
            "sf_update_record", "PATCH", f"/sobjects/{object_type}/{record_id}", json=fields
        )
        return f"Updated {object_type} {record_id} (HTTP {r.status_code})"
    except Exception as e:
        return _tool_error("sf_update_record", e)


@tool
def sf_list_opportunities(
    stage: Annotated[str, "Filter by stage name (optional, e.g. 'Prospecting', 'Closed Won')"] = "",
    limit: Annotated[int, "Max results to return"] = 20,
) -> str:
    """List Salesforce opportunities, optionally filtered by stage."""
    where = f"WHERE StageName = '{stage}'" if stage else ""
    soql = f"SELECT Id, Name, StageName, Amount, CloseDate, AccountId FROM Opportunity {where} ORDER BY CloseDate DESC LIMIT {limit}"
    return cast(str, sf_query.invoke({"soql": soql}))


@tool
def sf_search(
    query: Annotated[str, "SOSL search string"],
    objects: Annotated[
        str, "Comma-separated objects to search (default: Account,Contact,Lead)"
    ] = "Account,Contact,Lead",
    limit: Annotated[int, "Max results per object"] = 5,
) -> str:
    """Full-text search across Salesforce objects using SOSL."""
    try:
        returning = ", ".join(f"{o}(Id, Name LIMIT {limit})" for o in objects.split(","))
        sosl = f"FIND {{{query}}} IN ALL FIELDS RETURNING {returning}"
        r = _request("sf_search", "GET", "/search", params={"q": sosl})
        search_records = r.json().get("searchRecords", [])
        if not search_records:
            return f"No results for '{query}'."
        lines = [f"Search results for '{query}' ({len(search_records)}):"]
        for rec in search_records:
            lines.append(
                f"  [{rec.get('attributes', {}).get('type', '?')}] {rec.get('Id')} — {rec.get('Name', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return _tool_error("salesforce", e)


@tool
def sf_pipeline_summary() -> str:
    """Get a summary of the current Salesforce sales pipeline by stage."""
    soql = (
        "SELECT StageName, COUNT(Id) cnt, SUM(Amount) total "
        "FROM Opportunity WHERE IsClosed = false "
        "GROUP BY StageName ORDER BY SUM(Amount) DESC"
    )
    return cast(str, sf_query.invoke({"soql": soql}))


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
