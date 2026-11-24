"""FeishuLarkAgent tools — messaging and docs via Feishu/Lark API.

Auth env vars:
  FEISHU_APP_ID     — Feishu App ID
  FEISHU_APP_SECRET — Feishu App Secret
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential

logger = logging.getLogger(__name__)

_BASE = "https://open.feishu.cn/open-apis"
_token_cache: dict = {}


def _get_tenant_token() -> str:
    now = time.time()
    if _token_cache.get("expires_at", 0) > now + 60:
        return cast(str, _token_cache["token"])

    app_id = get_credential("FEISHU_APP_ID", "")
    app_secret = get_credential("FEISHU_APP_SECRET", "")
    if not app_id or not app_secret:
        raise ValueError("FEISHU_APP_ID and FEISHU_APP_SECRET must be set.")

    r = httpx.post(
        f"{_BASE}/auth/v3/tenant_access_token/internal",
        json={"app_id": app_id, "app_secret": app_secret},
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("tenant_access_token", "")
    expire = data.get("expire", 7200)
    _token_cache["token"] = token
    _token_cache["expires_at"] = now + expire
    return cast(str, token)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {_get_tenant_token()}",
        "Content-Type": "application/json",
    }


@tool
def feishu_send_message(
    receive_id: Annotated[str, "Chat ID or user open_id to send message to"],
    text: Annotated[str, "Message text content"],
    receive_id_type: Annotated[str, "Type: chat_id or open_id"] = "chat_id",
) -> str:
    """Send a text message via Feishu/Lark."""
    try:
        import json

        r = httpx.post(
            f"{_BASE}/im/v1/messages",
            headers=_headers(),
            params={"receive_id_type": receive_id_type},
            json={
                "receive_id": receive_id,
                "msg_type": "text",
                "content": json.dumps({"text": text}),
            },
            timeout=15,
        )
        r.raise_for_status()
        data = r.json().get("data", {})
        return f"Message sent: message_id={data.get('message_id', '?')}"
    except Exception as e:
        return f"[tool-error] feishu: {e}"


@tool
def feishu_list_chats(
    limit: Annotated[int, "Max chats to return"] = 20,
) -> str:
    """List Feishu group chats the bot is a member of."""
    try:
        r = httpx.get(
            f"{_BASE}/im/v1/chats",
            headers=_headers(),
            params={"page_size": limit},
            timeout=15,
        )
        r.raise_for_status()
        chats = r.json().get("data", {}).get("items", [])
        if not chats:
            return "No chats found."
        lines = [f"Feishu chats ({len(chats)}):"]
        for c in chats:
            lines.append(
                f"  [{c['chat_id']}] {c.get('name', 'unnamed')} — {c.get('chat_type', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] feishu: {e}"


@tool
def feishu_get_messages(
    chat_id: Annotated[str, "Chat ID to retrieve messages from"],
    limit: Annotated[int, "Max messages to return"] = 20,
) -> str:
    """Get recent messages from a Feishu chat."""
    try:
        r = httpx.get(
            f"{_BASE}/im/v1/messages",
            headers=_headers(),
            params={"container_id_type": "chat", "container_id": chat_id, "page_size": limit},
            timeout=15,
        )
        r.raise_for_status()
        msgs = r.json().get("data", {}).get("items", [])
        if not msgs:
            return "No messages found."
        import json

        lines = [f"Messages in chat {chat_id} ({len(msgs)}):"]
        for m in reversed(msgs):
            sender = m.get("sender", {}).get("id", "?")
            try:
                content = json.loads(m.get("body", {}).get("content", "{}"))
                text = content.get("text", m.get("body", {}).get("content", ""))
            except Exception:
                text = m.get("body", {}).get("content", "")
            ts = m.get("create_time", "")[:10]
            lines.append(f"  [{ts}] {sender}: {text[:100]}")
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] feishu: {e}"


@tool
def feishu_create_doc(
    title: Annotated[str, "Document title"],
    content: Annotated[str, "Initial document content (plain text)"] = "",
    folder_token: Annotated[str, "Parent folder token (optional)"] = "",
) -> str:
    """Create a new Feishu document."""
    try:
        payload: dict = {"title": title}
        if folder_token:
            payload["folder_token"] = folder_token

        r = httpx.post(
            f"{_BASE}/docx/v1/documents",
            headers=_headers(),
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        doc = r.json().get("data", {}).get("document", {})
        doc_id = doc.get("document_id", "")
        url = f"https://feishu.cn/docx/{doc_id}"
        return f"Document created: {title}\nID: {doc_id}\nURL: {url}"
    except Exception as e:
        return f"[tool-error] feishu: {e}"


@tool
def feishu_search_messages(
    query: Annotated[str, "Search query"],
    limit: Annotated[int, "Max results"] = 10,
) -> str:
    """Search Feishu messages."""
    try:
        r = httpx.post(
            f"{_BASE}/search/v2/message",
            headers=_headers(),
            json={"query": query, "page_size": limit},
            timeout=15,
        )
        r.raise_for_status()
        items = r.json().get("data", {}).get("items", [])
        if not items:
            return f"No results for '{query}'."
        lines = [f"Feishu message search results ({len(items)}):"]
        for item in items:
            msg = item.get("message", {})
            lines.append(
                f"  [{msg.get('create_time', '')[:10]}] {msg.get('body', {}).get('content', '')[:80]}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"[tool-error] feishu: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
