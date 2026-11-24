"""Notion tools for the NotionAgent subagent."""

from __future__ import annotations

import logging
from typing import Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_NOTION_CREDS_MSG = "[missing-credentials] NOTION_API_KEY (or NOTION_TOKEN) not set in vault."


def _notion_client() -> tuple[Any, str | None]:
    """Return (client, error_string). One is always None."""
    notion_mod, err = optional_import("notion_client", purpose="Notion workspace access")
    if err:
        return None, err
    token = get_credential("NOTION_API_KEY") or get_credential("NOTION_TOKEN")
    if not token:
        return None, _NOTION_CREDS_MSG
    assert notion_mod is not None  # narrowed: err is None ⇒ import succeeded
    return notion_mod.Client(auth=token), None


@tool
def notion_search(query: str, limit: int = 10) -> str:
    """Search Notion workspace for pages and databases matching the query."""
    client, err = _notion_client()
    if err:
        return err
    try:
        results = client.search(query=query, page_size=limit).get("results", [])
        lines = []
        for r in results:
            title = ""
            props = r.get("properties", {})
            if props.get("title"):
                title_parts = props["title"].get("title", [])
                title = "".join(p.get("plain_text", "") for p in title_parts)
            elif props.get("Name"):
                name_parts = props["Name"].get("title", [])
                title = "".join(p.get("plain_text", "") for p in name_parts)
            obj_type = r.get("object", "unknown")
            url = r.get("url", "")
            lines.append(f"[{obj_type}] {title or r.get('id')} — {url}")
        return "\n".join(lines) or "No results found."
    except Exception as exc:
        return f"[tool-error] notion_search: {exc}"


@tool
def notion_get_page(page_id: str) -> str:
    """Get content of a Notion page by ID."""
    client, err = _notion_client()
    if err:
        return err
    try:
        blocks = client.blocks.children.list(block_id=page_id).get("results", [])
        lines = []
        for block in blocks:
            btype = block.get("type", "")
            data = block.get(btype, {})
            rich_text = data.get("rich_text", [])
            text = "".join(rt.get("plain_text", "") for rt in rich_text)
            if text:
                lines.append(text)
        return "\n".join(lines) or "(no text content)"
    except Exception as exc:
        return f"[tool-error] notion_get_page: {exc}"


@tool
def notion_create_page(
    parent_page_id: str,
    title: str,
    content: str = "",
) -> str:
    """Create a new Notion page under a parent page."""
    client, err = _notion_client()
    if err:
        return err
    try:
        new_page = client.pages.create(
            parent={"type": "page_id", "page_id": parent_page_id},
            properties={"title": {"title": [{"text": {"content": title}}]}},
        )
        page_id = new_page.get("id", "")
        if content and page_id:
            client.blocks.children.append(
                block_id=page_id,
                children=[
                    {
                        "object": "block",
                        "type": "paragraph",
                        "paragraph": {
                            "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
                        },
                    }
                ],
            )
        return f"Created page '{title}': {new_page.get('url', page_id)}"
    except Exception as exc:
        return f"[tool-error] notion_create_page: {exc}"


@tool
def notion_append_to_page(page_id: str, content: str) -> str:
    """Append a paragraph of text to an existing Notion page."""
    client, err = _notion_client()
    if err:
        return err
    try:
        client.blocks.children.append(
            block_id=page_id,
            children=[
                {
                    "object": "block",
                    "type": "paragraph",
                    "paragraph": {
                        "rich_text": [{"type": "text", "text": {"content": content[:2000]}}]
                    },
                }
            ],
        )
        return f"Appended to page {page_id}."
    except Exception as exc:
        return f"[tool-error] notion_append_to_page: {exc}"


@tool
def notion_list_databases(limit: int = 10) -> str:
    """List Notion databases in the workspace."""
    client, err = _notion_client()
    if err:
        return err
    try:
        results = client.search(
            filter={"value": "database", "property": "object"}, page_size=limit
        ).get("results", [])
        lines = []
        for db in results:
            title_parts = db.get("title", [])
            title = "".join(p.get("plain_text", "") for p in title_parts)
            lines.append(f"{title or db.get('id', '?')} — {db.get('url', '')}")
        return "\n".join(lines) or "No databases found."
    except Exception as exc:
        return f"[tool-error] notion_list_databases: {exc}"


@tool
def notion_query_database(database_id: str, limit: int = 25) -> str:
    """Query a Notion database and return its rows (basic property summary)."""
    client, err = _notion_client()
    if err:
        return err
    try:
        results = client.databases.query(database_id=database_id, page_size=limit).get(
            "results", []
        )
        if not results:
            return "No rows found."
        lines = [f"Notion database rows ({len(results)}):"]
        for row in results:
            props = row.get("properties", {})
            title = ""
            for v in props.values():
                if v.get("type") == "title":
                    title = "".join(p.get("plain_text", "") for p in v.get("title", []))
                    break
            lines.append(f"- [{row.get('id', '?')}] {title or '(untitled)'} {row.get('url', '')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] notion_query_database: {exc}"


@tool
def notion_update_page_property(
    page_id: str,
    property_name: str,
    value: str,
) -> str:
    """Update a Notion page property (rich_text or title)."""
    client, err = _notion_client()
    if err:
        return err
    try:
        client.pages.update(
            page_id=page_id,
            properties={
                property_name: {"rich_text": [{"type": "text", "text": {"content": value}}]}
            },
        )
        return f"Updated {property_name} on {page_id}."
    except Exception as exc:
        return f"[tool-error] notion_update_page_property: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
