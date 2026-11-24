"""Mailchimp email-marketing tools.

Auth env vars: MAILCHIMP_API_KEY, MAILCHIMP_SERVER (e.g. us1)
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


def _mc_base() -> str:
    server = get_credential("MAILCHIMP_SERVER", "us1")
    return f"https://{server}.api.mailchimp.com/3.0"


def _mc_auth() -> tuple[str, str]:
    return ("anystring", get_credential("MAILCHIMP_API_KEY", ""))


@tool
def mailchimp_list_audiences() -> str:
    """List Mailchimp audiences (mailing lists)."""
    r = httpx.get(f"{_mc_base()}/lists", auth=_mc_auth(), params={"count": 50}, timeout=10)
    r.raise_for_status()
    lists = r.json().get("lists", [])
    lines = [f"- [{l['id']}] {l['name']} ({l['stats']['member_count']} members)" for l in lists]
    return "\n".join(lines) if lines else "No audiences found."


@tool
def mailchimp_add_subscriber(
    list_id: Annotated[str, "Mailchimp audience ID"],
    email: Annotated[str, "Subscriber email address"],
    first_name: Annotated[str, "First name"] = "",
    last_name: Annotated[str, "Last name"] = "",
) -> str:
    """Add a subscriber to a Mailchimp audience."""
    payload = {
        "email_address": email,
        "status": "subscribed",
        "merge_fields": {"FNAME": first_name, "LNAME": last_name},
    }
    r = httpx.post(
        f"{_mc_base()}/lists/{list_id}/members",
        auth=_mc_auth(),
        json=payload,
        timeout=10,
    )
    if r.status_code == 400 and "already a list member" in r.text:
        return f"{email} is already subscribed."
    r.raise_for_status()
    return f"Subscribed {email} to audience {list_id}."


@tool
def mailchimp_create_campaign(
    list_id: Annotated[str, "Mailchimp audience ID"],
    subject: Annotated[str, "Email subject line"],
    from_name: Annotated[str, "Sender display name"],
    reply_to: Annotated[str, "Reply-to email address"],
    title: Annotated[str, "Campaign title (internal)"] = "",
) -> str:
    """Create a Mailchimp email campaign (draft)."""
    payload = {
        "type": "regular",
        "recipients": {"list_id": list_id},
        "settings": {
            "subject_line": subject,
            "from_name": from_name,
            "reply_to": reply_to,
            "title": title or subject,
        },
    }
    r = httpx.post(f"{_mc_base()}/campaigns", auth=_mc_auth(), json=payload, timeout=10)
    r.raise_for_status()
    campaign = r.json()
    return f"Campaign created: [{campaign['id']}] '{campaign['settings']['title']}' (status: draft)"


@tool
def mailchimp_send_campaign(
    campaign_id: Annotated[str, "Mailchimp campaign ID to send"],
) -> str:
    """Send a Mailchimp campaign (must be in ready-to-send state)."""
    r = httpx.post(
        f"{_mc_base()}/campaigns/{campaign_id}/actions/send",
        auth=_mc_auth(),
        timeout=15,
    )
    if r.status_code == 204:
        return f"Campaign {campaign_id} sent successfully."
    r.raise_for_status()
    return f"Campaign {campaign_id} sent."


@tool
def mailchimp_get_analytics(
    campaign_id: Annotated[str, "Mailchimp campaign ID"],
) -> str:
    """Get open rate, click rate, and other analytics for a Mailchimp campaign."""
    r = httpx.get(
        f"{_mc_base()}/reports/{campaign_id}",
        auth=_mc_auth(),
        timeout=10,
    )
    r.raise_for_status()
    rep = r.json()
    return (
        f"Campaign: {rep.get('campaign_title', campaign_id)}\n"
        f"Emails sent: {rep.get('emails_sent', 0)}\n"
        f"Open rate: {rep.get('open_rate', 0):.1%}\n"
        f"Click rate: {rep.get('click_rate', 0):.1%}\n"
        f"Unsubscribes: {rep.get('unsubscribed', 0)}\n"
        f"Bounces: {rep.get('bounces', {}).get('hard_bounces', 0) + rep.get('bounces', {}).get('soft_bounces', 0)}"
    )


get_tools, get_tool = make_toolkit_api(globals())
