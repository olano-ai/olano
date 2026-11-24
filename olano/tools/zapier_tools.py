"""Zapier tools — NLA (Natural Language Actions) + Zap management.

Auth: ZAPIER_NLA_API_KEY env var for NLA endpoint.
      ZAPIER_API_KEY for Zap management (separate key from zapier.com/developer).
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_NLA_BASE = "https://nla.zapier.com/api/v1"
_MGMT_BASE = "https://api.zapier.com/v1"
_NLA_CREDS_MSG = "[missing-credentials] ZAPIER_NLA_API_KEY not set in vault."
_MGMT_CREDS_MSG = "[missing-credentials] ZAPIER_API_KEY not set in vault."


def _nla_headers() -> tuple[dict | None, str | None]:
    key = get_credential("ZAPIER_NLA_API_KEY", "")
    if not key:
        return None, _NLA_CREDS_MSG
    return {"x-api-key": key}, None


def _mgmt_headers() -> tuple[dict | None, str | None]:
    key = get_credential("ZAPIER_API_KEY", "")
    if not key:
        return None, _MGMT_CREDS_MSG
    return {"Authorization": f"Bearer {key}"}, None


@tool
def zapier_list_actions() -> str:
    """List available Zapier NLA (Natural Language Actions) for the authenticated user."""
    headers, err = _nla_headers()
    if err:
        return err
    try:
        r = httpx.get(f"{_NLA_BASE}/exposed/", headers=headers, timeout=15)
        r.raise_for_status()
        actions = r.json().get("results", [])
        lines = [
            f"- [{a.get('operation_id', '?')}] {a.get('description', '?')}" for a in actions[:20]
        ]
        return "\n".join(lines) if lines else "No NLA actions configured."
    except Exception as exc:
        return f"[tool-error] zapier_list_actions: {exc}"


@tool
def zapier_nla_action(
    action_id: Annotated[str, "The NLA action operation_id to execute"],
    instructions: Annotated[str, "Plain-English instructions for the action"],
) -> str:
    """Execute a Zapier NLA action using natural language instructions.

    Example: action_id='gmail_send_email', instructions='Send an email to alice@example.com saying the report is ready'
    """
    headers, err = _nla_headers()
    if err:
        return err
    try:
        payload = {"instructions": instructions, "preview_only": False}
        r = httpx.post(
            f"{_NLA_BASE}/exposed/{action_id}/execute/",
            headers=headers,
            json=payload,
            timeout=30,
        )
        r.raise_for_status()
        result = r.json()
        status = result.get("status", "unknown")
        return f"Zapier NLA action '{action_id}': {status}\n{result.get('review_url', '')}"
    except Exception as exc:
        return f"[tool-error] zapier_nla_action: {exc}"


@tool
def zapier_list_zaps() -> str:
    """List all Zaps in the user's Zapier account."""
    headers, err = _mgmt_headers()
    if err:
        return err
    try:
        r = httpx.get(f"{_MGMT_BASE}/zaps/", headers=headers, timeout=15)
        r.raise_for_status()
        zaps = r.json().get("objects", [])
        lines = [
            f"- [{z.get('id', '?')}] {z.get('title', '?')} "
            f"({'on' if z.get('status') == 'on' else 'off'})"
            for z in zaps[:20]
        ]
        return "\n".join(lines) if lines else "No zaps found."
    except Exception as exc:
        return f"[tool-error] zapier_list_zaps: {exc}"


@tool
def zapier_run_zap(
    zap_id: Annotated[str, "Zap ID to trigger"],
    payload: Annotated[dict | None, "JSON payload to send as trigger data"] = None,
) -> str:
    """Trigger a Zapier Zap (must have a webhook trigger configured)."""
    try:
        hook_url = f"https://hooks.zapier.com/hooks/catch/{zap_id}/"
        r = httpx.post(hook_url, json=payload or {}, timeout=15)
        r.raise_for_status()
        return f"Zap {zap_id} triggered. Status: {r.status_code}"
    except Exception as exc:
        return f"[tool-error] zapier_run_zap: {exc}"


@tool
def zapier_enable_zap(zap_id: Annotated[str, "Zap ID to enable"]) -> str:
    """Enable (turn on) a Zapier Zap."""
    headers, err = _mgmt_headers()
    if err:
        return err
    try:
        r = httpx.patch(
            f"{_MGMT_BASE}/zaps/{zap_id}/",
            headers=headers,
            json={"status": "on"},
            timeout=15,
        )
        r.raise_for_status()
        return f"Zap {zap_id} enabled."
    except Exception as exc:
        return f"[tool-error] zapier_enable_zap: {exc}"


@tool
def zapier_disable_zap(zap_id: Annotated[str, "Zap ID to disable"]) -> str:
    """Disable (turn off) a Zapier Zap."""
    headers, err = _mgmt_headers()
    if err:
        return err
    try:
        r = httpx.patch(
            f"{_MGMT_BASE}/zaps/{zap_id}/",
            headers=headers,
            json={"status": "off"},
            timeout=15,
        )
        r.raise_for_status()
        return f"Zap {zap_id} disabled."
    except Exception as exc:
        return f"[tool-error] zapier_disable_zap: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
