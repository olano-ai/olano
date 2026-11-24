"""Workspace health scoring utilities."""

from __future__ import annotations

from pathlib import Path


def compute_health(workspace_root: Path) -> dict:
    """Standalone health check without full cartography run."""
    from olano.cartography.cartographer import WorkspaceCartographer
    from olano.config.workspace import Workspace

    ws = Workspace(root=workspace_root)
    cart = WorkspaceCartographer(ws)
    cart._phase_scan_and_classify()
    return cart._phase_health()
