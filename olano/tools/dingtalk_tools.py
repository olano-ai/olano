"""DingTalkAgent tools — messaging via DingTalk API.

Auth env vars:
  DINGTALK_APP_KEY    — DingTalk app key
  DINGTALK_APP_SECRET — DingTalk app secret
  DINGTALK_ROBOT_TOKEN — Webhook robot token (for simple group notifications)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import time
import urllib.parse
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_BASE = "https://oapi.dingtalk.com"
_token_cache: dict = {}


def _get_access_token() -> str:
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return cast(str, _token_cache["token"])

    app_key = get_credential("DINGTALK_APP_KEY", "")
    app_secret = get_credential("DINGTALK_APP_SECRET", "")
    if not app_key or not app_secret:
        raise ValueError("DINGTALK_APP_KEY and DINGTALK_APP_SECRET must be set.")

    r = httpx.get(
        f"{_BASE}/gettoken",
        params={"appkey": app_key, "appsecret": app_secret},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token", "")
    expire = data.get("expires_in", 7200)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expire
    return cast(str, token)


def _robot_webhook_url() -> str:
    token = get_credential("DINGTALK_ROBOT_TOKEN", "")
    secret = get_credential("DINGTALK_ROBOT_SECRET", "")
    if not token:
        raise ValueError("DINGTALK_ROBOT_TOKEN not set.")

    url = f"https://oapi.dingtalk.com/robot/send?access_token={token}"
    if secret:
        timestamp = str(round(time.time() * 1000))
        string_to_sign = f"{timestamp}\n{secret}"
        sign = base64.b64encode(
            hmac.new(secret.encode(), string_to_sign.encode(), digestmod=hashlib.sha256).digest()
        ).decode()
        url += f"&timestamp={timestamp}&sign={urllib.parse.quote_plus(sign)}"
    return url


@tool
def dingtalk_send_robot_message(
    text: Annotated[str, "Message text to send via robot webhook"],
    at_all: Annotated[bool, "Whether to @all in the group"] = False,
    at_mobiles: Annotated[str, "Comma-separated mobile numbers to @mention"] = "",
) -> str:
    """Send a text message to a DingTalk group via robot webhook."""
    try:
        mobiles = [m.strip() for m in at_mobiles.split(",") if m.strip()]
        payload = {
            "msgtype": "text",
            "text": {"content": text},
            "at": {"isAtAll": at_all, "atMobiles": mobiles},
        }
        r = httpx.post(_robot_webhook_url(), json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) == 0:
            return "Message sent successfully."
        return f"DingTalk error {data.get('errcode')}: {data.get('errmsg')}"
    except Exception as e:
        return f"[tool-error] dingtalk: {e}"


@tool
def dingtalk_send_markdown(
    title: Annotated[str, "Message title"],
    markdown: Annotated[str, "Markdown content"],
    at_all: Annotated[bool, "Whether to @all"] = False,
) -> str:
    """Send a Markdown message to a DingTalk group via robot webhook."""
    try:
        payload = {
            "msgtype": "markdown",
            "markdown": {"title": title, "text": markdown},
            "at": {"isAtAll": at_all},
        }
        r = httpx.post(_robot_webhook_url(), json=payload, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) == 0:
            return "Markdown message sent."
        return f"DingTalk error {data.get('errcode')}: {data.get('errmsg')}"
    except Exception as e:
        return f"[tool-error] dingtalk: {e}"


@tool
def dingtalk_get_department_list(
    dept_id: Annotated[int, "Parent department ID (1 = root)"] = 1,
) -> str:
    """List DingTalk departments."""
    try:
        r = httpx.get(
            f"{_BASE}/department/list",
            params={"access_token": _get_access_token(), "id": dept_id},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        depts = data.get("department", [])
        if not depts:
            return "No departments found."
        lines = ["DingTalk departments:"]
        for d in depts:
            lines.append(f"  [{d['id']}] {d['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] dingtalk: {e}"


@tool
def dingtalk_get_user_list(
    dept_id: Annotated[int, "Department ID to list users from"] = 1,
    limit: Annotated[int, "Max users to return"] = 20,
) -> str:
    """List users in a DingTalk department."""
    try:
        r = httpx.get(
            f"{_BASE}/user/simplelist",
            params={"access_token": _get_access_token(), "department_id": dept_id, "size": limit},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        users = data.get("userlist", [])
        if not users:
            return "No users found."
        lines = [f"Users in dept {dept_id} ({len(users)}):"]
        for u in users:
            lines.append(f"  [{u['userid']}] {u.get('name', '?')}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] dingtalk: {e}"


@tool
def dingtalk_send_work_notification(
    user_id_list: Annotated[str, "Comma-separated user IDs to notify"],
    title: Annotated[str, "Notification title"],
    text: Annotated[str, "Notification body text"],
    agent_id: Annotated[str, "DingTalk app agent ID (from DINGTALK_AGENT_ID env or provided)"] = "",
) -> str:
    """Send a work notification to specific users via DingTalk app."""
    try:
        aid = agent_id or get_credential("DINGTALK_AGENT_ID", "")
        if not aid:
            return "Error: DINGTALK_AGENT_ID not set."

        r = httpx.post(
            f"{_BASE}/topapi/message/corpconversation/asyncsend_v2",
            params={"access_token": _get_access_token()},
            json={
                "agent_id": int(aid),
                "userid_list": user_id_list,
                "msg": {
                    "msgtype": "text",
                    "text": {"content": f"{title}\n{text}"},
                },
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) == 0:
            return f"Work notification sent. task_id={data.get('task_id', '?')}"
        return f"Error {data.get('errcode')}: {data.get('errmsg')}"
    except Exception as e:
        return f"[tool-error] dingtalk: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
