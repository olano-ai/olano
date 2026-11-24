"""Airtable tools for the AirtableAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_AIRTABLE_CREDS_MSG = "[missing-credentials] AIRTABLE_API_KEY (or AIRTABLE_TOKEN) not set in vault."


def _airtable_request(method: str, path: str, data: dict | None = None) -> tuple[Any, str | None]:
    """Return (json_response, error_string). One is always None."""
    requests_mod, err = optional_import("requests", purpose="Airtable API access")
    if err:
        return None, err
    assert requests_mod is not None
    token = get_credential("AIRTABLE_API_KEY") or get_credential("AIRTABLE_TOKEN")
    if not token:
        return None, _AIRTABLE_CREDS_MSG
    try:
        base_url = "https://api.airtable.com/v0"
        resp = requests_mod.request(
            method,
            f"{base_url}/{path}",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json=data,
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json(), None
    except Exception as exc:
        return None, f"[tool-error] Airtable {method} {path}: {exc}"


def _parse_fields(fields: str) -> dict[str, Any]:
    field_dict: dict[str, Any] = {}
    for pair in fields.split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            field_dict[k.strip()] = v.strip()
    return field_dict


@tool
def airtable_list_records(base_id: str, table_name: str, limit: int = 10) -> str:
    """List records from an Airtable table."""
    data, err = _airtable_request("GET", f"{base_id}/{table_name}?maxRecords={limit}")
    if err:
        return err
    records = (data or {}).get("records", [])
    lines = []
    for r in records:
        fields = r.get("fields", {})
        summary = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:5])
        lines.append(f"[{r.get('id', '?')}] {summary}")
    return "\n".join(lines) or "No records found."


@tool
def airtable_create_record(base_id: str, table_name: str, fields: str) -> str:
    """Create a record in an Airtable table. fields='Name=John, Status=Active' (comma-separated)."""
    field_dict = _parse_fields(fields)
    data, err = _airtable_request("POST", f"{base_id}/{table_name}", {"fields": field_dict})
    if err:
        return err
    return f"Created record {(data or {}).get('id', '?')}"


@tool
def airtable_update_record(base_id: str, table_name: str, record_id: str, fields: str) -> str:
    """Update an Airtable record. fields='Status=Done, Priority=High'."""
    field_dict = _parse_fields(fields)
    data, err = _airtable_request(
        "PATCH", f"{base_id}/{table_name}/{record_id}", {"fields": field_dict}
    )
    if err:
        return err
    return f"Updated record {(data or {}).get('id', '?')}"


@tool
def airtable_search_records(base_id: str, table_name: str, formula: str, limit: int = 10) -> str:
    """Search Airtable records using a formula. formula='AND({Status}=\"Active\",{Priority}=\"High\")'."""
    import urllib.parse

    path = (
        f"{base_id}/{table_name}?filterByFormula={urllib.parse.quote(formula)}&maxRecords={limit}"
    )
    data, err = _airtable_request("GET", path)
    if err:
        return err
    records = (data or {}).get("records", [])
    lines = []
    for r in records:
        fields = r.get("fields", {})
        summary = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:5])
        lines.append(f"[{r.get('id', '?')}] {summary}")
    return "\n".join(lines) or "No matching records."


@tool
def airtable_delete_record(base_id: str, table_name: str, record_id: str) -> str:
    """Delete a single Airtable record by ID."""
    data, err = _airtable_request("DELETE", f"{base_id}/{table_name}/{record_id}")
    if err:
        return err
    return f"Deleted record {(data or {}).get('id', record_id)}"


@tool
def airtable_get_record(base_id: str, table_name: str, record_id: str) -> str:
    """Fetch a single Airtable record by ID with all field values."""
    data, err = _airtable_request("GET", f"{base_id}/{table_name}/{record_id}")
    if err:
        return err
    if not data:
        return "Record not found."
    fields = data.get("fields", {})
    lines = [f"Record {data.get('id', '?')}:"]
    for k, v in fields.items():
        lines.append(f"  {k}: {v}")
    return "\n".join(lines)


get_tools, get_tool = make_toolkit_api(globals())
