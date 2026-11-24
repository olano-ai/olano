"""Small authenticated JSON client shared by the Asian social toolkits.

The three platforms expose different (and account/region dependent) API
products.  Keeping transport here makes the agent tools testable and allows an
approved partner gateway to be selected with ``<PLATFORM>_API_BASE_URL``.
"""

from __future__ import annotations

import json
from typing import Any

import requests

from olano.tools._credentials import get_credential


def api_request(
    platform: str,
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
) -> str:
    """Call a platform (or approved partner) API and return readable JSON."""
    prefix = platform.upper()
    token = get_credential(f"{prefix}_ACCESS_TOKEN", "").strip()
    base_url = get_credential(f"{prefix}_API_BASE_URL", "").strip().rstrip("/")
    if not token or not base_url:
        return (
            f"[missing-credentials] Set {prefix}_ACCESS_TOKEN and "
            f"{prefix}_API_BASE_URL in the vault. The base URL may be the "
            "platform's approved API product or your approved partner gateway."
        )
    try:
        response = requests.request(
            method,
            f"{base_url}/{path.lstrip('/')}",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            params=params,
            json=payload,
            timeout=30,
        )
        response.raise_for_status()
        if not response.content:
            return json.dumps({"ok": True}, ensure_ascii=False)
        data = response.json()
        return json.dumps(data, ensure_ascii=False, indent=2)
    except requests.HTTPError as exc:
        body = exc.response.text[:1000] if exc.response is not None else ""
        return f"[tool-error] {platform} API HTTP error: {exc}; response={body}"
    except (requests.RequestException, ValueError) as exc:
        return f"[tool-error] {platform} API: {exc}"
