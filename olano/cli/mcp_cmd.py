"""Helpers for the ``olano mcp`` command group.

Thin glue over :mod:`olano.admin.mcp` so the click commands in
:mod:`olano.cli.main` stay declarative. Covers parsing repeatable
``KEY=VALUE`` options and driving the interactive OAuth flow (localhost
redirect capture) from a terminal.
"""

from __future__ import annotations

import asyncio
from typing import Any

import click


def parse_kv_pairs(pairs: tuple[str, ...], *, label: str) -> dict[str, str]:
    """Parse repeatable ``KEY=VALUE`` CLI options into a dict.

    Raises ``click.BadParameter`` on a malformed entry (no ``=``).
    """
    out: dict[str, str] = {}
    for raw in pairs:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise click.BadParameter(f"{label} must be KEY=VALUE, got: {raw!r}")
        out[key.strip()] = value
    return out


def authorize_server(agent: str, url: str, *, open_browser: bool = True) -> dict[str, Any]:
    """Run the interactive OAuth flow for a configured server and print progress.

    Returns the terminal flow-session payload. Prints the authorization URL the
    user must open (and, when *open_browser*, opens it automatically).
    """
    from olano import admin

    def _on_url(auth_url: str) -> None:
        click.echo()
        click.echo("  Open this URL to authorize (a browser may open automatically):")
        click.echo(f"    {auth_url}")
        click.echo()
        click.echo("  Waiting for the authorization redirect…")

    return asyncio.run(
        admin.authorize_mcp_server(
            agent,
            url,
            on_authorization_url=_on_url,
            open_browser=open_browser,
        )
    )
