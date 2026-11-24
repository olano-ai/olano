"""Composio meta-toolkit — unlocks 500+ third-party integrations.

Auth env var: COMPOSIO_API_KEY (sign up at composio.dev).

Composio is a meta-platform: a single API key gives access to actions across
Notion, Linear, Slack, GitHub, Calendly, Pipedrive, Intercom, Klaviyo, and
hundreds of other apps. Each app's actions are exposed by string name (e.g.
``GITHUB_CREATE_AN_ISSUE``). This toolkit gives the agent three thin wrappers:
discover available actions, inspect one, and execute one by name.

For high-traffic actions you can later bind them as named olano tools, but
the open-ended pattern below is the recommended starting point.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_COMPOSIO_CREDS_MSG = "[missing-credentials] COMPOSIO_API_KEY not set in vault."


def _composio_client() -> tuple[Any, str | None]:
    """Return (client, error_string)."""
    composio, err = optional_import("composio", purpose="Composio integration platform")
    if err:
        # Try the older package layout
        composio, err2 = optional_import("composio_core", purpose="Composio integration platform")
        if err2:
            return None, err
    key = get_credential("COMPOSIO_API_KEY")
    if not key:
        return None, _COMPOSIO_CREDS_MSG
    try:
        Composio = getattr(composio, "Composio", None) or getattr(composio, "ComposioToolSet", None)
        if Composio is None:
            return None, "[tool-error] composio: SDK shape not recognized (no Composio class)."
        return Composio(api_key=key), None
    except Exception as exc:
        return None, f"[tool-error] composio init: {exc}"


@tool
def composio_list_actions(
    app: Annotated[
        str,
        "Filter by app name (e.g. 'github', 'notion', 'slack'). Empty = all apps.",
    ] = "",
    search: Annotated[str, "Optional substring filter on action name/description"] = "",
    limit: Annotated[int, "Max actions to list"] = 30,
) -> str:
    """List available Composio actions, optionally filtered by app or keyword."""
    client, err = _composio_client()
    if err:
        return err
    try:
        # The composio SDK exposes a few different surface shapes across versions.
        # Try the newer "actions.list" path first, fall back to "get_actions".
        actions: list[Any]
        if hasattr(client, "actions") and hasattr(client.actions, "list"):
            actions = list(client.actions.list(app=app or None))
        elif hasattr(client, "get_actions"):
            actions = list(client.get_actions(apps=[app]) if app else client.get_actions())
        else:
            return "[tool-error] composio_list_actions: SDK does not expose actions listing."

        if search:
            s = search.lower()
            actions = [
                a
                for a in actions
                if s in str(getattr(a, "name", "")).lower()
                or s in str(getattr(a, "description", "")).lower()
            ]
        if not actions:
            return f"No Composio actions match app='{app}' search='{search}'."
        lines = [f"Composio actions ({len(actions)}, showing {min(limit, len(actions))}):"]
        for a in actions[:limit]:
            name = getattr(a, "name", "?") or (a.get("name") if isinstance(a, dict) else "?")
            desc = getattr(a, "description", "") or (
                a.get("description") if isinstance(a, dict) else ""
            )
            lines.append(f"  - {name}: {(desc or '')[:120]}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] composio_list_actions: {exc}"


@tool
def composio_describe_action(
    action_name: Annotated[str, "Action name (e.g. 'GITHUB_CREATE_AN_ISSUE')"],
) -> str:
    """Describe a Composio action's parameters and return shape."""
    client, err = _composio_client()
    if err:
        return err
    try:
        if hasattr(client, "actions") and hasattr(client.actions, "get"):
            action = client.actions.get(action_name)
        elif hasattr(client, "get_action_schemas"):
            schemas = client.get_action_schemas(actions=[action_name])
            action = schemas[0] if schemas else None
        else:
            return "[tool-error] composio_describe_action: SDK does not expose action schemas."
        if not action:
            return f"No Composio action '{action_name}'."
        params = getattr(action, "parameters", None) or (
            action.get("parameters") if isinstance(action, dict) else {}
        )
        desc = getattr(action, "description", "") or (
            action.get("description") if isinstance(action, dict) else ""
        )
        return f"Action: {action_name}\n  {desc}\n  Parameters: {params}"
    except Exception as exc:
        return f"[tool-error] composio_describe_action: {exc}"


@tool
def composio_run_action(
    action_name: Annotated[str, "Action name (e.g. 'GITHUB_CREATE_AN_ISSUE')"],
    params_json: Annotated[str, "JSON string with action parameters"],
    connected_account_id: Annotated[
        str, "Composio connected_account_id (empty = default connection for the app)"
    ] = "",
) -> str:
    """Execute a Composio action with the given parameters.

    Example:
      action_name='GITHUB_CREATE_AN_ISSUE'
      params_json='{"owner":"foo","repo":"bar","title":"Bug","body":"…"}'
    """
    client, err = _composio_client()
    if err:
        return err
    try:
        import json

        params = json.loads(params_json) if params_json else {}
    except Exception as exc:
        return f"[tool-error] composio_run_action: invalid params_json: {exc}"
    try:
        if hasattr(client, "actions") and hasattr(client.actions, "execute"):
            kwargs: dict[str, Any] = {"action": action_name, "params": params}
            if connected_account_id:
                kwargs["connected_account_id"] = connected_account_id
            result = client.actions.execute(**kwargs)
        elif hasattr(client, "execute_action"):
            result = client.execute_action(
                action=action_name,
                params=params,
                connected_account_id=connected_account_id or None,
            )
        else:
            return "[tool-error] composio_run_action: SDK does not expose execute API."
        return f"Composio {action_name} →\n{result}"
    except Exception as exc:
        return f"[tool-error] composio_run_action: {exc}"


@tool
def composio_list_connected_accounts(
    app: Annotated[str, "Filter by app name (empty = all)"] = "",
) -> str:
    """List Composio connected accounts (OAuth links) available to the API key."""
    client, err = _composio_client()
    if err:
        return err
    try:
        if hasattr(client, "connected_accounts") and hasattr(client.connected_accounts, "list"):
            accounts = list(client.connected_accounts.list(app=app or None))
        elif hasattr(client, "get_connected_accounts"):
            accounts = list(client.get_connected_accounts())
        else:
            return (
                "[tool-error] composio_list_connected_accounts: SDK does not expose "
                "connected-accounts listing."
            )
        if not accounts:
            return f"No connected accounts for app='{app}'."
        lines = [f"Composio connected accounts ({len(accounts)}):"]
        for a in accounts:
            aid = getattr(a, "id", None) or (a.get("id") if isinstance(a, dict) else "?")
            app_name = getattr(a, "appName", None) or (
                a.get("appName") if isinstance(a, dict) else "?"
            )
            status = getattr(a, "status", None) or (a.get("status") if isinstance(a, dict) else "?")
            lines.append(f"  - [{aid}] {app_name} ({status})")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] composio_list_connected_accounts: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
