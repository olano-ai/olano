"""WeCom (WeChat Work) tools — enterprise messaging via WeCom API.

Auth env vars:
  WECOM_CORP_ID     — Enterprise Corp ID
  WECOM_AGENT_SECRET — Agent secret for your app
  WECOM_AGENT_ID    — Agent ID (app ID within corp)
  WECOM_ROBOT_KEY   — Group robot webhook key (for simple group bots)
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_BASE = "https://qyapi.weixin.qq.com/cgi-bin"
_token_cache: dict = {}


def _get_access_token() -> str:
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return cast(str, _token_cache["token"])

    corp_id = get_credential("WECOM_CORP_ID", "")
    secret = get_credential("WECOM_AGENT_SECRET", "")
    if not corp_id or not secret:
        raise ValueError("WECOM_CORP_ID and WECOM_AGENT_SECRET must be set.")

    r = httpx.get(
        f"{_BASE}/gettoken",
        params={"corpid": corp_id, "corpsecret": secret},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("access_token", "")
    expire = data.get("expires_in", 7200)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expire
    return cast(str, token)


def _agent_id() -> str:
    aid = get_credential("WECOM_AGENT_ID", "")
    if not aid:
        raise ValueError("WECOM_AGENT_ID not set.")
    return aid


@tool
def wecom_send_message(
    to_user: Annotated[str, "Recipient user ID (use '@all' for all users)"],
    content: Annotated[str, "Message content"],
    msg_type: Annotated[str, "Message type: text or markdown"] = "text",
) -> str:
    """Send a message to a WeCom user or all users."""
    try:
        payload: dict = {
            "touser": to_user,
            "msgtype": msg_type,
            "agentid": int(_agent_id()),
        }
        if msg_type == "text":
            payload["text"] = {"content": content}
        elif msg_type == "markdown":
            payload["markdown"] = {"content": content}

        r = httpx.post(
            f"{_BASE}/message/send",
            params={"access_token": _get_access_token()},
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) == 0:
            return "Message sent."
        return f"WeCom error {data.get('errcode')}: {data.get('errmsg')}"
    except Exception as e:
        return f"[tool-error] wecom: {e}"


@tool
def wecom_send_robot_message(
    content: Annotated[str, "Message text to send via group robot"],
    mentioned_list: Annotated[str, "Comma-separated @user IDs (optional)"] = "",
) -> str:
    """Send a text message to a WeCom group via robot webhook."""
    robot_key = get_credential("WECOM_ROBOT_KEY", "")
    if not robot_key:
        return "[missing-credentials] WECOM_ROBOT_KEY not set."

    try:
        mentioned = [m.strip() for m in mentioned_list.split(",") if m.strip()]
        payload = {
            "msgtype": "text",
            "text": {
                "content": content,
                "mentioned_list": mentioned,
            },
        }
        r = httpx.post(
            f"https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key={robot_key}",
            json=payload,
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errcode", 0) == 0:
            return "Robot message sent."
        return f"[tool-error] wecom_send_robot_message: {data.get('errcode')}: {data.get('errmsg')}"
    except Exception as e:
        return f"[tool-error] wecom_send_robot_message: {e}"


@tool
def wecom_list_departments() -> str:
    """List WeCom organization departments."""
    try:
        r = httpx.get(
            f"{_BASE}/department/list",
            params={"access_token": _get_access_token()},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        depts = data.get("department", [])
        if not depts:
            return "No departments found."
        lines = ["WeCom departments:"]
        for d in depts:
            lines.append(f"  [{d['id']}] {d['name']} (parent: {d.get('parentid', 0)})")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] wecom: {e}"


@tool
def wecom_list_users(
    dept_id: Annotated[int, "Department ID to list users from"] = 1,
    fetch_child: Annotated[bool, "Include users in child departments"] = True,
) -> str:
    """List WeCom users in a department."""
    try:
        r = httpx.get(
            f"{_BASE}/user/simplelist",
            params={
                "access_token": _get_access_token(),
                "department_id": dept_id,
                "fetch_child": 1 if fetch_child else 0,
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        users = data.get("userlist", [])
        if not users:
            return "No users found."
        lines = [f"WeCom users in dept {dept_id} ({len(users)}):"]
        for u in users:
            lines.append(f"  [{u['userid']}] {u.get('name', '?')}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] wecom: {e}"


@tool
def wecom_get_user_info(
    user_id: Annotated[str, "WeCom user ID"],
) -> str:
    """Get detailed info for a WeCom user."""
    try:
        r = httpx.get(
            f"{_BASE}/user/get",
            params={"access_token": _get_access_token(), "userid": user_id},
            timeout=10,
        )
        r.raise_for_status()
        u = r.json()
        if u.get("errcode", 0) != 0:
            return f"Error {u.get('errcode')}: {u.get('errmsg')}"
        return (
            f"User: {u.get('name')} ({user_id})\n"
            f"  Mobile: {u.get('mobile', 'N/A')}\n"
            f"  Email: {u.get('email', 'N/A')}\n"
            f"  Dept IDs: {u.get('department', [])}\n"
            f"  Position: {u.get('position', 'N/A')}\n"
            f"  Status: {'Active' if u.get('status') == 1 else 'Inactive'}"
        )
    except Exception as e:
        return f"[tool-error] wecom: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
