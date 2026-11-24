"""Resolve an agent's workspace root, cheaply enough to do per request.

Resolving the root parses the agent's json5 config (~0.5s of CPU for a large
one), and a single page view triggers one request per asset, so the resolved
root is cached per agent and invalidated by the config file's path and mtime (a
stat per request is fine; a re-parse per request is not). Shared by the viewer
route and the page supervisor.
"""

from __future__ import annotations

from pathlib import Path

_WS_ROOT_CACHE: dict[str, tuple[str, float, Path]] = {}


def agent_workspace_root(agent: str) -> Path | None:
    """``agent``'s workspace root, or ``None`` when the agent has no config."""
    from olano.config.loader import find_agent_config_path, load_agent_config
    from olano.config.workspace import get_workspace

    path = find_agent_config_path(agent)
    if path is None:
        return None
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    hit = _WS_ROOT_CACHE.get(agent)
    if hit is not None and hit[0] == str(path) and hit[1] == mtime:
        return hit[2]
    try:
        config = load_agent_config(agent)
        root = get_workspace(config.workspace).root
    except FileNotFoundError:
        return None
    _WS_ROOT_CACHE[agent] = (str(path), mtime, root)
    return root
