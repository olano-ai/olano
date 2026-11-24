"""Shared Meta (Facebook + Instagram) client core.

Private helper for :mod:`olano.tools.facebook_tools` and
:mod:`olano.tools.instagram_tools`. It is **not** a toolkit (no ``get_tools``)
— it is the shared abstraction layer those two surfaces sit on top of:

    Agent
      │  facebook_* / instagram_* tools
      ▼
    _meta_client.py            ← you are here: auth, version pin, graph escape hatch
      ▼
    facebook_business (Meta Business SDK)
      ▼
    Graph API

We use the Meta Business SDK for object access (Page, IGUser, IGMedia, …) and
expose a thin :func:`graph_get` / :func:`graph_post` escape hatch for endpoints
not cleanly covered by SDK adobjects (e.g. ``ig_hashtag_search``).

Auth — read from the vault-backed environment:

* ``META_ACCESS_TOKEN`` (required) — long-lived Page / IG user access token.
* ``META_PAGE_ID``      (optional) — default Facebook Page id.
* ``META_IG_USER_ID``   (optional) — default Instagram Business/Creator user id.

Credential resolution is isolated in :func:`_resolve_token` so a future OAuth /
token-refresh layer can replace it without touching tool code.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

# Single place for the yearly Graph API version bump (≈2-year support window).
GRAPH_API_VERSION = "v22.0"

MISSING_CREDS_MSG = (
    "[missing-credentials] META_ACCESS_TOKEN not set in vault. Provide a long-lived "
    "Page/IG user access token (and optionally META_PAGE_ID / META_IG_USER_ID)."
)

# Graph API error-code families that mean "you are being throttled".
_RATE_LIMIT_CODES = {4, 17, 32, 613}


def _resolve_token() -> str:
    """Return the Meta access token, or '' if unset. Single auth seam."""
    return get_credential("META_ACCESS_TOKEN", "").strip()


def default_page_id() -> str:
    return get_credential("META_PAGE_ID", "").strip()


def default_ig_user_id() -> str:
    return get_credential("META_IG_USER_ID", "").strip()


def get_api() -> tuple[Any | None, str | None]:
    """Initialize the SDK and return ``(api, None)`` or ``(None, error_string)``.

    Tools call this first: a non-None error string is the missing-credentials or
    init-failure message to return straight to the agent.
    """
    token = _resolve_token()
    if not token:
        return None, MISSING_CREDS_MSG
    try:
        from facebook_business.api import FacebookAdsApi

        api = FacebookAdsApi.init(access_token=token, api_version=GRAPH_API_VERSION)
        return api, None
    except Exception as exc:
        return None, f"[tool-error] meta client init: {exc}"


def meta_error(exc: Exception) -> str:
    """Format any SDK/Graph exception into the standard ``[tool-error]`` string."""
    try:
        from facebook_business.exceptions import FacebookRequestError

        if isinstance(exc, FacebookRequestError):
            code = exc.api_error_code()
            msg = exc.api_error_message()
            if code in _RATE_LIMIT_CODES:
                return f"[tool-error] Meta API rate limit (code {code}): {msg}"
            return f"[tool-error] Meta API error {code}: {msg}"
    except Exception:
        pass
    return f"[tool-error] Meta API: {exc}"


def _segments(path: str) -> tuple[str, ...]:
    """Split a graph path into the token tuple the SDK's ``call`` expects."""
    return tuple(p for p in str(path).strip("/").split("/") if p)


def graph_get(path: str, params: dict | None = None) -> tuple[dict | None, str | None]:
    """GET escape hatch for endpoints the SDK adobjects don't cleanly model.

    Returns ``(json_dict, None)`` or ``(None, error_string)``.
    """
    api, err = get_api()
    if err or api is None:
        return None, err
    try:
        resp = api.call("GET", _segments(path), params or {})
        return resp.json(), None
    except Exception as exc:
        return None, meta_error(exc)


def graph_post(path: str, data: dict | None = None) -> tuple[dict | None, str | None]:
    """POST escape hatch. Returns ``(json_dict, None)`` or ``(None, error_string)``."""
    api, err = get_api()
    if err or api is None:
        return None, err
    try:
        resp = api.call("POST", _segments(path), data or {})
        return resp.json(), None
    except Exception as exc:
        return None, meta_error(exc)
