"""``cognition.approval_required`` — what Fully Autonomous must never sweep.

The per-agent list ``cognition.approval_required`` (default
``["external_send", "irreversible", "identity_edit"]``) names categories of
change that always wait for a human, **whatever** ``auto_approve`` says. This
module is the single definition of what falls into each category, shared by
the two places autonomous approval happens so they can never disagree:

- :func:`olano.cognition.orchestrator.auto_approve_cognition_items` — the
  Fully Autonomous sweep over pending Pulse items. :func:`item_categories`
  classifies a :class:`~olano.pulse.models.PulseItem` by the files its action
  would write.
- :func:`olano.core.runner._resolve_autonomous_decisions` — HITL decisions for
  tool calls made on a cognition-rooted turn (a background worker).
  :func:`action_categories` classifies an interrupt action by its tool's HITL
  category and, for filesystem writes, by the path it targets.

Category semantics, as documented to customers in the user guide:

``identity_edit``
    Rewrites who the agent is: any write whose target is ``SOUL.md``,
    ``AGENTS.md`` or ``IDENTITY.md`` (matched on the file's basename, so a
    workspace-relative or absolute path both count). ``MEMORY.md`` is memory,
    not identity, and is deliberately outside this set.
``external_send``
    Anything that hands content to a system outside the deployment — the
    ``write_external`` HITL category (email, calendar, posts, pushes, A2A…).
``irreversible``
    Anything that cannot be undone — the ``financial`` HITL category (money
    movement) and ``secret_write`` (overwriting a stored credential destroys
    the previous value).

An unknown name in the list is harmless: it matches nothing. The lossless
workspace tidy-up phase (``cognition/workspace_optimizer.py``) is out of scope
by design — it consolidates identity files under a snapshot with one-click undo
rather than changing what they say, and it never passes through approval.
"""

from __future__ import annotations

import json
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from olano.security.hitl_policy import tool_category

#: Files whose rewrite is an ``identity_edit``. Basenames, case-sensitive —
#: these are the canonical spellings the workspace scaffold writes.
IDENTITY_FILES: frozenset[str] = frozenset({"SOUL.md", "AGENTS.md", "IDENTITY.md"})

#: The full category vocabulary. Kept as a tuple so docs and the config default
#: can be checked against one list.
KNOWN_CATEGORIES: tuple[str, ...] = ("external_send", "irreversible", "identity_edit")

# approval_required category → HITL tool categories (hitl_policy.CATEGORY_TOOLS)
# it holds back on a cognition-rooted turn. ``identity_edit`` is path-based and
# handled separately in :func:`action_categories`.
_HITL_CATEGORY_MAP: dict[str, frozenset[str]] = {
    "external_send": frozenset({"write_external"}),
    "irreversible": frozenset({"financial", "secret_write"}),
}

# Argument names a filesystem write tool uses for its target. deepagents'
# ``write_file``/``edit_file`` take ``file_path``; the others are defensive.
_PATH_ARG_KEYS: tuple[str, ...] = ("file_path", "path", "target_path", "filename")

# Pulse action types that write a single ``path``.
_PATH_ACTIONS: frozenset[str] = frozenset({"append_lines", "append_text"})


def normalise_categories(raw: Any) -> frozenset[str]:
    """Lower-cased, stripped, de-duplicated category names from a config value.

    Accepts a list/tuple/set of strings; anything else (``None``, a bare
    string) yields the empty set rather than raising, so a malformed
    ``approval_required`` degrades to "hold nothing back" loudly at the
    config-validation layer rather than silently mid-sweep.
    """
    if not isinstance(raw, list | tuple | set | frozenset):
        return frozenset()
    return frozenset(str(c).strip().lower() for c in raw if str(c).strip())


def is_identity_path(raw_path: Any) -> bool:
    """True when ``raw_path`` names one of :data:`IDENTITY_FILES`.

    Matches on the basename only: ``SOUL.md``, ``./SOUL.md``,
    ``/abs/workspace/SOUL.md`` and ``C:\\ws\\SOUL.md`` all count.
    """
    if not raw_path:
        return False
    text = str(raw_path).strip()
    if not text:
        return False
    # Try both flavours so a Windows-style path from a tool arg is not missed.
    for cls in (PurePosixPath, PureWindowsPath):
        try:
            if cls(text).name in IDENTITY_FILES:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _coerce_args(args: Any) -> dict[str, Any]:
    """Interrupt-action ``args`` arrive as a dict or a JSON string (see
    ``runner._extract_action_command``); normalise to a dict."""
    if isinstance(args, dict):
        return args
    if isinstance(args, str):
        raw = args.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except Exception:
                return {}
            return parsed if isinstance(parsed, dict) else {}
    return {}


def _target_path(args: dict[str, Any]) -> str:
    for key in _PATH_ARG_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def action_categories(tool_name: str, args: Any = None) -> frozenset[str]:
    """``approval_required`` categories an interrupt action falls into.

    ``tool_name`` is the tool the agent asked to call; ``args`` its arguments
    (dict or JSON string). A tool in no HITL category and not targeting an
    identity file returns the empty set.
    """
    cats: set[str] = set()
    hitl_cat = tool_category(tool_name)
    if hitl_cat is not None:
        for ours, theirs in _HITL_CATEGORY_MAP.items():
            if hitl_cat in theirs:
                cats.add(ours)
    if hitl_cat == "write_fs" and is_identity_path(_target_path(_coerce_args(args))):
        cats.add("identity_edit")
    return frozenset(cats)


def _action_paths(action: dict[str, Any] | None) -> list[str]:
    """Every file path a Pulse action would write, recursing into composites."""
    if not isinstance(action, dict):
        return []
    kind = str(action.get("type") or "")
    if kind in _PATH_ACTIONS:
        path = action.get("path")
        return [str(path)] if path else []
    if kind == "composite":
        out: list[str] = []
        for step in action.get("steps") or []:
            out.extend(_action_paths(step if isinstance(step, dict) else None))
        return out
    return []


def item_categories(item: Any) -> frozenset[str]:
    """``approval_required`` categories a pending Pulse item falls into.

    Classified by what approving it would *write*: the ``path`` of an
    ``append_lines``/``append_text`` action (recursing through ``composite``
    steps) and the producer's ``metadata.target_file`` hint. A ``run_worker``
    action is not classified here — the worker's own tool calls are decided
    per call by the runner, which applies :func:`action_categories`.
    """
    cats: set[str] = set()
    action = getattr(item, "action", None)
    metadata = getattr(item, "metadata", None) or {}
    paths = _action_paths(action)
    hint = metadata.get("target_file") if isinstance(metadata, dict) else None
    if hint:
        paths.append(str(hint))
    if any(is_identity_path(p) for p in paths):
        cats.add("identity_edit")
    return frozenset(cats)


def held_by(categories: frozenset[str], required: frozenset[str]) -> frozenset[str]:
    """The subset of ``categories`` that ``required`` says must wait for a human."""
    return categories & required
