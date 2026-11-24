"""Append-only audit log for cognition/pulse actions.

Records what the autonomous system and the user did — optimizations, undos,
pulse approvals/rejections — so there is a durable, inspectable history (the
dashboard "Activity" view reads this; it is also a plain JSONL file on disk).

Layout: ``<workspace>/.olano/state/cognition/actions.jsonl`` — one JSON object
per line, newest appended at the end. Bounded to the most recent entries so it
never grows without limit.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.workspace import WORKSPACE_STATE_DIRNAME

logger = logging.getLogger(__name__)

_MAX_ENTRIES = 500


def _log_path(workspace_root: Path) -> Path:
    return workspace_root / WORKSPACE_STATE_DIRNAME / "state" / "cognition" / "actions.jsonl"


def log_action(
    workspace_root: Path,
    action: str,
    *,
    summary: str = "",
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry. Never raises — logging must not break a request."""
    try:
        path = _log_path(workspace_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "summary": summary,
            "detail": detail or {},
        }
        lines: list[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(entry))
        # Keep only the most recent entries.
        if len(lines) > _MAX_ENTRIES:
            lines = lines[-_MAX_ENTRIES:]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("audit.log_action failed: %s", exc)


def read_actions(workspace_root: Path, limit: int = 100) -> list[dict[str, Any]]:
    """Return the most recent audit entries, newest first."""
    path = _log_path(workspace_root)
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except OSError:
        return []
    out.reverse()
    return out[:limit]
