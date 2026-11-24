"""First-run admin onboarding state (deployment-wide, not per-user).

Tracks whether the guided dashboard onboarding wizard — shown to the first
admin who signs in to a deployed (multi-user) instance — has been completed
or skipped. The state is a small JSON file in the Olano home so it
survives restarts and applies to the whole deployment: once any admin
finishes (or skips) the tour, no admin sees it again.

Distinct from the *agent identity* onboarding (the IDENTITY.md first-run
sentinel, read by ``core/system_middleware.py``). This module is purely about
the dashboard's first-login setup wizard.

Kept import-light (no runtime/auth imports) so both ``runtime/auth.py`` and
tests can use it without cycles.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from olano.config.home import get_olano_home
from olano.platforms.helpers import atomic_json_write

logger = logging.getLogger(__name__)

_STATE_FILENAME = "onboarding.json"

# Completion is terminal (only reset() undoes it), so once a state path is
# known-completed we skip the disk read — /api/auth/me calls this on every
# auth transition and must not re-read a file whose answer is fixed. Keyed by
# resolved path so per-test LIONCLAW_HOME swaps never see each other's state.
# Consequence: deleting onboarding.json by hand doesn't re-arm a live server —
# use DELETE /api/onboarding (reset()) or restart the runtime.
_COMPLETED_PATHS: set[str] = set()

# Step ids the wizard reports; unknown keys are dropped on write so the state
# file can't be used as an arbitrary-JSON dumping ground.
# One panel each: welcome → your profile (claim the provisioned account +
# rekey the vault) → AI provider (credits / BYOK) → meet Agent Father. The
# retired ids (password, vault, account, agent, telegram, search, experts)
# posted by an older bundle are dropped like any unknown key.
KNOWN_STEPS = (
    "welcome",
    "profile",
    "provider",
    "agentfather",
)
_STEP_OUTCOMES = ("done", "skipped", "pending")


def state_path() -> Path:
    return get_olano_home() / _STATE_FILENAME


def load_state() -> dict[str, Any]:
    """Return the persisted onboarding state ({} when never completed)."""
    path = state_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:  # pragma: no cover - corrupt file → treat as fresh
        logger.warning("Unreadable onboarding state at %s; treating as fresh", path)
        return {}


def is_completed() -> bool:
    path = str(state_path())
    if path in _COMPLETED_PATHS:
        return True
    done = bool(load_state().get("completed"))
    if done:
        _COMPLETED_PATHS.add(path)
    return done


def _sanitize_steps(steps: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(steps, dict):
        return {}
    clean: dict[str, str] = {}
    for key in KNOWN_STEPS:
        value = steps.get(key)
        if isinstance(value, str) and value in _STEP_OUTCOMES:
            clean[key] = value
    return clean


def mark_completed(
    user_id: str, steps: dict[str, Any] | None = None, skipped: bool = False
) -> dict[str, Any]:
    """Persist completion atomically and return the new state."""
    state = {
        "completed": True,
        "skipped": bool(skipped),
        "completed_by": user_id,
        "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "steps": _sanitize_steps(steps),
    }
    path = state_path()
    atomic_json_write(path, state)
    _COMPLETED_PATHS.add(str(path))
    return state


def reset() -> None:
    """Forget completion so the wizard shows again (admin 're-run tour')."""
    path = state_path()
    _COMPLETED_PATHS.discard(str(path))
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def required_for(is_admin: bool) -> bool:
    """Whether the wizard must be shown to this principal right now.

    Every install's admin is onboarded — multi-user deployments and
    single-user/dev boxes alike (there the implicit owner counts as the
    admin): the wizard covers work every box needs (the owner's account +
    password, the vault passphrase, AI access, meeting Agent Father), and on a
    single-user box its profile step *creates* the admin sign-in, securing a
    previously open dashboard. Members are never gated — they have nothing to
    configure. One completion/skip clears it deployment-wide.
    """
    return bool(is_admin and not is_completed())
