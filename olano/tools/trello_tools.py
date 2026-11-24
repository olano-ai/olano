"""Trello tools — boards, lists, cards, checklists.

Auth: TRELLO_API_KEY + TRELLO_TOKEN env vars.
"""

from __future__ import annotations

import logging
from typing import Annotated, cast

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_BASE = "https://api.trello.com/1"
_TRELLO_CREDS_MSG = "[missing-credentials] TRELLO_API_KEY and TRELLO_TOKEN must be set in vault."


def _creds_check() -> str | None:
    if not get_credential("TRELLO_API_KEY") or not get_credential("TRELLO_TOKEN"):
        return _TRELLO_CREDS_MSG
    return None


def _params(**extra) -> dict:
    return {
        "key": get_credential("TRELLO_API_KEY", ""),
        "token": get_credential("TRELLO_TOKEN", ""),
        **extra,
    }


def _get(path: str, **params):
    r = httpx.get(f"{_BASE}{path}", params=_params(**params), timeout=15)
    r.raise_for_status()
    return r.json()


def _post(path: str, **data) -> dict:
    r = httpx.post(f"{_BASE}{path}", params=_params(), json=data, timeout=15)
    r.raise_for_status()
    return cast(dict, r.json())


def _put(path: str, **data) -> dict:
    r = httpx.put(f"{_BASE}{path}", params=_params(), json=data, timeout=15)
    r.raise_for_status()
    return cast(dict, r.json())


@tool
def trello_board_list() -> str:
    """List all Trello boards for the authenticated user."""
    err = _creds_check()
    if err:
        return err
    try:
        boards = _get("/members/me/boards", fields="name,id,url,closed")
        open_boards = [b for b in boards if not b.get("closed")]
        lines = [
            f"- [{b.get('name', '?')}] id={b.get('id', '?')} url={b.get('url', '?')}"
            for b in open_boards
        ]
        return "\n".join(lines) if lines else "No open boards found."
    except Exception as exc:
        return f"[tool-error] trello_board_list: {exc}"


@tool
def trello_list_cards(
    board_id: Annotated[str, "Trello board ID"],
    list_name: Annotated[str, "Filter by list name (optional, empty = all)"] = "",
) -> str:
    """List cards on a Trello board, optionally filtered by list name."""
    err = _creds_check()
    if err:
        return err
    try:
        lists = _get(f"/boards/{board_id}/lists", cards="open")
        result = []
        for lst in lists:
            name = lst.get("name", "")
            if list_name and list_name.lower() not in name.lower():
                continue
            cards = lst.get("cards", [])
            if cards:
                result.append(f"**{name}** ({len(cards)} cards)")
                for c in cards[:10]:
                    due_val = c.get("due") or ""
                    due = f" [due: {due_val[:10]}]" if due_val else ""
                    result.append(f"  - {c.get('name', '?')}{due} id={c.get('id', '?')}")
        return "\n".join(result) if result else "No cards found."
    except Exception as exc:
        return f"[tool-error] trello_list_cards: {exc}"


@tool
def trello_card_create(
    list_id: Annotated[str, "Trello list ID to add card to"],
    name: Annotated[str, "Card title"],
    desc: Annotated[str, "Card description"] = "",
    due: Annotated[str, "Due date ISO string (optional)"] = "",
) -> str:
    """Create a new card in a Trello list."""
    err = _creds_check()
    if err:
        return err
    try:
        data: dict = {"idList": list_id, "name": name}
        if desc:
            data["desc"] = desc
        if due:
            data["due"] = due
        card = _post("/cards", **data)
        return (
            f"Card created: [{card.get('name', '?')}] id={card.get('id', '?')} "
            f"url={card.get('url', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] trello_card_create: {exc}"


@tool
def trello_card_move(
    card_id: Annotated[str, "Trello card ID"],
    list_id: Annotated[str, "Target list ID"],
) -> str:
    """Move a Trello card to a different list."""
    err = _creds_check()
    if err:
        return err
    try:
        card = _put(f"/cards/{card_id}", idList=list_id)
        return f"Card '{card.get('name', '?')}' moved to list {list_id}."
    except Exception as exc:
        return f"[tool-error] trello_card_move: {exc}"


@tool
def trello_card_update(
    card_id: Annotated[str, "Trello card ID"],
    name: Annotated[str, "New name (optional)"] = "",
    desc: Annotated[str, "New description (optional)"] = "",
    due: Annotated[str, "Due date ISO string (optional)"] = "",
    closed: Annotated[bool, "Archive the card"] = False,
) -> str:
    """Update a Trello card's name, description, due date, or archive it."""
    err = _creds_check()
    if err:
        return err
    try:
        data: dict = {}
        if name:
            data["name"] = name
        if desc:
            data["desc"] = desc
        if due:
            data["due"] = due
        if closed:
            data["closed"] = True
        card = _put(f"/cards/{card_id}", **data)
        return f"Card updated: [{card.get('name', '?')}] id={card.get('id', '?')}"
    except Exception as exc:
        return f"[tool-error] trello_card_update: {exc}"


@tool
def trello_add_comment(
    card_id: Annotated[str, "Trello card ID"],
    text: Annotated[str, "Comment text"],
) -> str:
    """Add a comment to a Trello card."""
    err = _creds_check()
    if err:
        return err
    try:
        _post(f"/cards/{card_id}/actions/comments", text=text)
        return f"Comment added to card {card_id}."
    except Exception as exc:
        return f"[tool-error] trello_add_comment: {exc}"


@tool
def trello_add_checklist(
    card_id: Annotated[str, "Trello card ID"],
    name: Annotated[str, "Checklist name"],
    items: Annotated[list[str], "List of checklist item names"],
) -> str:
    """Add a checklist with items to a Trello card."""
    err = _creds_check()
    if err:
        return err
    try:
        checklist = _post("/checklists", idCard=card_id, name=name)
        cl_id = checklist.get("id")
        if not cl_id:
            return "[tool-error] trello_add_checklist: missing checklist id in response."
        for item in items:
            _post(f"/checklists/{cl_id}/checkItems", name=item, checked=False)
        return f"Checklist '{name}' with {len(items)} items added to card {card_id}."
    except Exception as exc:
        return f"[tool-error] trello_add_checklist: {exc}"


@tool
def trello_add_label(
    card_id: Annotated[str, "Trello card ID"],
    label_id: Annotated[str, "Label ID to attach"],
) -> str:
    """Attach an existing label to a Trello card."""
    err = _creds_check()
    if err:
        return err
    try:
        _post(f"/cards/{card_id}/idLabels", value=label_id)
        return f"Label {label_id} attached to card {card_id}."
    except Exception as exc:
        return f"[tool-error] trello_add_label: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
