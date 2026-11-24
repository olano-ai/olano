"""Canva API tools."""

from __future__ import annotations

from typing import Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

_CANVA_BASE = "https://api.canva.com/rest/v1"
_CANVA_CREDS_MSG = "[missing-credentials] CANVA_ACCESS_TOKEN not set in vault."


def _headers() -> tuple[dict[str, str] | None, str | None]:
    token = get_credential("CANVA_ACCESS_TOKEN")
    if not token:
        return None, _CANVA_CREDS_MSG
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, None


def _request(method: str, path: str, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
    headers, err = _headers()
    if err:
        return None, err
    try:
        r = httpx.request(
            method,
            f"{_CANVA_BASE}/{path.lstrip('/')}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        r.raise_for_status()
        return (r.json() if r.content else {}), None
    except Exception as exc:
        return None, f"[tool-error] Canva {method} {path}: {exc}"


@tool
def canva_list_designs(query: str = "", limit: int = 20) -> str:
    """List or search Canva designs."""
    params: dict[str, Any] = {"limit": limit}
    if query:
        params["query"] = query
    data, err = _request("GET", "designs", params=params)
    if err:
        return err
    designs = (data or {}).get("items") or (data or {}).get("designs") or []
    if not designs:
        return "No Canva designs found."
    return "\n".join(
        f"- [{d.get('id')}] {d.get('title') or d.get('name', '?')} {d.get('url', '')}"
        for d in designs
    )


@tool
def canva_get_design(design_id: str) -> str:
    """Get Canva design metadata by ID."""
    data, err = _request("GET", f"designs/{design_id}")
    if err:
        return err
    return str(data)


@tool
def canva_create_design(title: str, design_type: str = "presentation") -> str:
    """Create a blank Canva design draft."""
    data, err = _request(
        "POST", "designs", json={"design_type": {"type": design_type}, "title": title}
    )
    if err:
        return err
    design = (data or {}).get("design", data or {})
    return f"Created Canva design {design.get('id')} {design.get('url', '')}"


@tool
def canva_create_export(design_id: str, file_type: str = "pdf") -> str:
    """Start an export job for a Canva design."""
    data, err = _request(
        "POST", "exports", json={"design_id": design_id, "format": {"type": file_type}}
    )
    if err:
        return err
    job = (data or {}).get("job", data or {})
    return f"Created Canva export job {job.get('id')} status={job.get('status')}"


@tool
def canva_get_export(export_id: str) -> str:
    """Get a Canva export job status and download URLs."""
    data, err = _request("GET", f"exports/{export_id}")
    if err:
        return err
    return str(data)


@tool
def canva_list_folders(limit: int = 20) -> str:
    """List Canva folders."""
    data, err = _request("GET", "folders", params={"limit": limit})
    if err:
        return err
    folders = (data or {}).get("items") or (data or {}).get("folders") or []
    if not folders:
        return "No Canva folders found."
    return "\n".join(f"- [{f.get('id')}] {f.get('name') or f.get('title', '?')}" for f in folders)


@tool
def canva_list_assets(limit: int = 20) -> str:
    """List Canva assets/uploads."""
    data, err = _request("GET", "assets", params={"limit": limit})
    if err:
        return err
    assets = (data or {}).get("items") or (data or {}).get("assets") or []
    if not assets:
        return "No Canva assets found."
    return "\n".join(f"- [{a.get('id')}] {a.get('name') or a.get('title', '?')}" for a in assets)


get_tools, get_tool = make_toolkit_api(globals())
