"""DocuSign eSignature REST tools."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

_DS_CREDS_MSG = (
    "[missing-credentials] DOCUSIGN_ACCOUNT_ID and DOCUSIGN_ACCESS_TOKEN must be set in vault."
)


def _base_url() -> tuple[str | None, str | None]:
    base = get_credential("DOCUSIGN_BASE_URL", "https://demo.docusign.net/restapi")
    account_id = get_credential("DOCUSIGN_ACCOUNT_ID")
    if not account_id:
        return None, _DS_CREDS_MSG
    return f"{base.rstrip('/')}/v2.1/accounts/{account_id}", None


def _headers() -> tuple[dict[str, str] | None, str | None]:
    token = get_credential("DOCUSIGN_ACCESS_TOKEN")
    if not token:
        return None, _DS_CREDS_MSG
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, None


def _request(method: str, path: str, **kwargs: Any) -> tuple[dict[str, Any] | None, str | None]:
    base, err = _base_url()
    if err:
        return None, err
    headers, err = _headers()
    if err:
        return None, err
    try:
        r = httpx.request(
            method,
            f"{base}/{path.lstrip('/')}",
            headers=headers,
            timeout=30,
            **kwargs,
        )
        r.raise_for_status()
        return (r.json() if r.content else {}), None
    except Exception as exc:
        return None, f"[tool-error] DocuSign {method} {path}: {exc}"


@tool
def docusign_list_envelopes(
    from_date: str, status: str = "sent,delivered,completed", count: int = 20
) -> str:
    """List DocuSign envelopes changed since from_date (YYYY-MM-DD)."""
    data, err = _request(
        "GET", "envelopes", params={"from_date": from_date, "status": status, "count": count}
    )
    if err:
        return err
    envelopes = (data or {}).get("envelopes", [])
    if not envelopes:
        return "No envelopes found."
    return "\n".join(
        f"- [{e.get('envelopeId')}] {e.get('emailSubject')} status={e.get('status')}"
        for e in envelopes
    )


@tool
def docusign_get_envelope(envelope_id: str) -> str:
    """Get DocuSign envelope details."""
    data, err = _request("GET", f"envelopes/{envelope_id}")
    if err:
        return err
    return str(data)


@tool
def docusign_list_recipients(envelope_id: str) -> str:
    """List recipients for a DocuSign envelope."""
    data, err = _request("GET", f"envelopes/{envelope_id}/recipients")
    if err:
        return err
    return str(data)


@tool
def docusign_create_envelope(
    document_path: str,
    recipient_email: str,
    recipient_name: str,
    email_subject: str,
    status: str = "created",
) -> str:
    """Create a DocuSign envelope from a local PDF/document path; status can be created or sent."""
    try:
        path = Path(document_path).expanduser()
        if not path.exists():
            return f"[tool-error] docusign_create_envelope: file not found: {document_path}"
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        payload = {
            "emailSubject": email_subject,
            "documents": [
                {
                    "documentBase64": encoded,
                    "name": path.name,
                    "fileExtension": path.suffix.lstrip(".") or "pdf",
                    "documentId": "1",
                }
            ],
            "recipients": {
                "signers": [
                    {
                        "email": recipient_email,
                        "name": recipient_name,
                        "recipientId": "1",
                        "routingOrder": "1",
                        "tabs": {
                            "signHereTabs": [
                                {
                                    "anchorString": "/sn1/",
                                    "anchorUnits": "pixels",
                                    "anchorYOffset": "10",
                                    "anchorXOffset": "20",
                                }
                            ]
                        },
                    }
                ]
            },
            "status": status,
        }
        data, err = _request("POST", "envelopes", json=payload)
        if err:
            return err
        d = data or {}
        return f"Created DocuSign envelope {d.get('envelopeId')} status={d.get('status')}"
    except Exception as exc:
        return f"[tool-error] docusign_create_envelope: {exc}"


@tool
def docusign_send_envelope(envelope_id: str) -> str:
    """Send a previously-created DocuSign draft envelope."""
    data, err = _request("PUT", f"envelopes/{envelope_id}", json={"status": "sent"})
    if err:
        return err
    d = data or {}
    return f"Sent DocuSign envelope {d.get('envelopeId', envelope_id)} status={d.get('status', 'sent')}"


@tool
def docusign_void_envelope(envelope_id: str, reason: str = "Voided by agent") -> str:
    """Void an in-progress DocuSign envelope."""
    data, err = _request(
        "PUT",
        f"envelopes/{envelope_id}",
        json={"status": "voided", "voidedReason": reason},
    )
    if err:
        return err
    d = data or {}
    return f"Voided DocuSign envelope {envelope_id} status={d.get('status', 'voided')}"


get_tools, get_tool = make_toolkit_api(globals())
