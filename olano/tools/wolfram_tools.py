"""Wolfram Alpha toolkit — math, science, structured knowledge queries.

Auth env var: WOLFRAM_APP_ID (sign up at developer.wolframalpha.com, free tier).
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


@tool
def wolfram_query(
    query: Annotated[str, "Natural-language query (math, science, conversions, facts)"],
) -> str:
    """Run a Wolfram Alpha query and return the structured answer."""
    wa, err = optional_import("wolframalpha", purpose="Wolfram Alpha queries")
    if err:
        return err
    assert wa is not None
    app_id = get_credential("WOLFRAM_APP_ID") or get_credential("WOLFRAMALPHA_APP_ID")
    if not app_id:
        return "[missing-credentials] WOLFRAM_APP_ID not set in vault."
    try:
        client = wa.Client(app_id)
        res = client.query(query)
        if not getattr(res, "success", False):
            return f"Wolfram Alpha did not understand '{query}'."
        pods = []
        for pod in res.pods:
            title = getattr(pod, "title", "?")
            texts: list[str] = []
            for sub in getattr(pod, "subpods", []) or []:
                t = getattr(sub, "plaintext", None) or ""
                if t:
                    texts.append(t)
            if texts:
                pods.append(f"**{title}**\n  " + "\n  ".join(texts))
        if not pods:
            return f"Wolfram Alpha returned no textual pods for '{query}'."
        return "\n\n".join(pods)
    except Exception as exc:
        return f"[tool-error] wolfram_query: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
