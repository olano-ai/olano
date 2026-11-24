"""FastAPI-facing adapters for enforcement decisions.

403 with the structured :meth:`Decision.to_detail` payload for policy
denials; 503 for ``policy_unavailable`` (the counting layer failed — the
operation is rejected, not allowed; deployment-settings design record, §6).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from olano.deployment import enforcement

if TYPE_CHECKING:
    from collections.abc import Callable

    from olano.deployment.enforcement import Decision


def reject(decision: Decision) -> None:
    """Raise the HTTP error for a denied decision (no-op when allowed)."""
    if decision.allowed:
        return
    # Imported lazily to mirror api.py's guarded FastAPI import.
    from fastapi import HTTPException

    status = 503 if decision.code == "policy_unavailable" else 403
    raise HTTPException(status_code=status, detail=decision.to_detail())


def require_feature(name: str, *, label: str | None = None) -> Callable[[], None]:
    """Dependency factory: 403 when a deployment feature toggle is off.

    Usage: ``@app.get(..., dependencies=[Depends(require_feature("agent_config_raw"))])``
    """

    def _check() -> None:
        reject(enforcement.feature_decision(name, label=label))

    return _check
