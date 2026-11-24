"""LinkedIn tools.

Auth env var: LINKEDIN_ACCESS_TOKEN
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_LI_CREDS_MSG = "[missing-credentials] LINKEDIN_ACCESS_TOKEN not set in vault."


def _li_headers() -> tuple[dict | None, str | None]:
    token = get_credential("LINKEDIN_ACCESS_TOKEN", "")
    if not token:
        return None, _LI_CREDS_MSG
    return (
        {
            "Authorization": f"Bearer {token}",
            "X-Restli-Protocol-Version": "2.0.0",
            "LinkedIn-Version": "202401",
        },
        None,
    )


@tool
def linkedin_post(
    text: Annotated[str, "Post text content"],
    visibility: Annotated[str, "PUBLIC or CONNECTIONS"] = "PUBLIC",
) -> str:
    """Create a LinkedIn post."""
    headers, err = _li_headers()
    if err:
        return err
    try:
        me = httpx.get("https://api.linkedin.com/v2/userinfo", headers=headers, timeout=10)
        me.raise_for_status()
        person_id = me.json().get("sub", "")
        if not person_id:
            return "[tool-error] linkedin_post: userinfo did not return a sub id."
        author = f"urn:li:person:{person_id}"

        payload = {
            "author": author,
            "lifecycleState": "PUBLISHED",
            "specificContent": {
                "com.linkedin.ugc.ShareContent": {
                    "shareCommentary": {"text": text},
                    "shareMediaCategory": "NONE",
                }
            },
            "visibility": {"com.linkedin.ugc.MemberNetworkVisibility": visibility},
        }
        r = httpx.post(
            "https://api.linkedin.com/v2/ugcPosts",
            headers=headers,
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        return f"LinkedIn post created: {r.headers.get('x-restli-id', 'unknown')}"
    except Exception as exc:
        return f"[tool-error] linkedin_post: {exc}"


@tool
def linkedin_get_analytics(
    post_id: Annotated[str, "LinkedIn post URN or ID"],
) -> str:
    """Get analytics for a LinkedIn post (impressions, clicks, engagement)."""
    headers, err = _li_headers()
    if err:
        return err
    try:
        r = httpx.get(
            f"https://api.linkedin.com/v2/socialMetadata/{post_id}",
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        return (
            f"Impressions: {data.get('impressionCount', 0)}\n"
            f"Clicks: {data.get('clickCount', 0)}\n"
            f"Likes: {data.get('likeCount', 0)}\n"
            f"Comments: {data.get('commentCount', 0)}\n"
            f"Shares: {data.get('shareCount', 0)}"
        )
    except Exception as exc:
        return f"[tool-error] linkedin_get_analytics: {exc}"


@tool
def linkedin_get_profile() -> str:
    """Get the authenticated user's LinkedIn profile (basic info)."""
    headers, err = _li_headers()
    if err:
        return err
    try:
        r = httpx.get("https://api.linkedin.com/v2/userinfo", headers=headers, timeout=10)
        r.raise_for_status()
        d = r.json()
        return (
            f"LinkedIn profile:\n"
            f"  Name: {d.get('name', '?')}\n"
            f"  Email: {d.get('email', '?')}\n"
            f"  Sub: {d.get('sub', '?')}\n"
            f"  Locale: {d.get('locale', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] linkedin_get_profile: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
