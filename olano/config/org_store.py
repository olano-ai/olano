"""Central organization store — ``~/.olano/organization.yaml``.

This is the single source of truth for org-chart placement (manager / reports /
co-workers) and ``talk_to_agent`` messaging governance (input/output modes +
allow-lists + autogenerate). It supersedes the per-agent ``org:`` block in each
``agent.json5``: the first time the store is written it is **seeded** from those
blocks (one-time migration), and thereafter the central file wins.

Why central rather than per-agent:

* Manager ↔ report and co-worker links are inherently *mutual*. Keeping them in
  one file makes reciprocity a single atomic read-modify-write instead of N
  cross-file edits that can half-fail and drift.
* The runtime policy (``talk_to.py``), the prompt assembler, and the org-chart
  endpoint all read the same file, so a live edit takes effect without a
  restart.

The on-disk schema::

    agents:
      marketing:
        title: "Marketing Manager"
        manager: null
        reports: [content-writer]
        coworkers: [finance-bot]
        input:  { mode: everyone, list: [] }   # mode: everyone | none | list
        output: { mode: list, list: [finance-bot] }
        autogenerate: false
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from olano.config.agent_config import OrgConfig
from olano.config.home import get_olano_paths

logger = logging.getLogger(__name__)

# Cache parsed orgs keyed by file path → (mtime, {casefold_name: OrgConfig}).
# Policy checks (talk_to_agent) read on a hot path, so avoid re-parsing the YAML
# unless the file changed.
_CACHE: dict[str, tuple[float, dict[str, OrgConfig]]] = {}


def _norm(name: str | None) -> str:
    return (name or "").strip().casefold()


# ── entry ⇄ OrgConfig mapping ────────────────────────────────────────────────
def _entry_to_org(entry: dict[str, Any]) -> OrgConfig:
    inp = entry.get("input") or {}
    out = entry.get("output") or {}
    return OrgConfig(
        title=entry.get("title"),
        manager=entry.get("manager"),
        reports=list(entry.get("reports") or []),
        coworkers=list(entry.get("coworkers") or []),
        input_mode=inp.get("mode"),
        input_agents=list(inp.get("agents") or []),
        output_mode=out.get("mode"),
        output_agents=list(out.get("agents") or []),
        autogenerate=bool(entry.get("autogenerate", False)),
        mutating_from_manager_only=bool(entry.get("mutating_from_manager_only", False)),
    )


def _org_to_entry(org: OrgConfig) -> dict[str, Any]:
    """Serialize an OrgConfig to a YAML entry with the mode made explicit."""
    return {
        "title": org.title,
        "manager": org.manager,
        "reports": list(org.reports),
        "coworkers": list(org.coworkers),
        "input": {"mode": org.effective_mode("input"), "agents": list(org.input_agents)},
        "output": {"mode": org.effective_mode("output"), "agents": list(org.output_agents)},
        "autogenerate": bool(org.autogenerate),
        "mutating_from_manager_only": bool(org.mutating_from_manager_only),
    }


# ── canonical name resolution ────────────────────────────────────────────────
def _canonical_map() -> dict[str, str]:
    """Map casefolded agent name → its canonical on-disk name."""
    try:
        from olano.config.loader import list_agents

        return {_norm(n): n for n in list_agents()}
    except Exception:
        return {}


def _canon(name: str | None, cmap: dict[str, str]) -> str | None:
    if not name:
        return name
    return cmap.get(_norm(name), name)


def _canon_org(org: OrgConfig, cmap: dict[str, str]) -> OrgConfig:
    return org.model_copy(
        update={
            "manager": _canon(org.manager, cmap),
            "reports": [_canon(r, cmap) or r for r in org.reports],
            "coworkers": [_canon(c, cmap) or c for c in org.coworkers],
            "input_agents": [_canon(a, cmap) or a for a in org.input_agents],
            "output_agents": [_canon(a, cmap) or a for a in org.output_agents],
        }
    )


# ── file IO ──────────────────────────────────────────────────────────────────
def _read_raw(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("Failed to parse %s; treating as empty.", path, exc_info=True)
        return {}


def _write_raw(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    _CACHE.pop(str(path), None)


def _load_orgs(override: str | Path | None = None) -> dict[str, OrgConfig]:
    """Return the central entries keyed by casefolded name (cached by mtime)."""
    path = get_olano_paths(override).org_path
    if not path.exists():
        return {}
    mtime = path.stat().st_mtime
    cached = _CACHE.get(str(path))
    if cached and cached[0] == mtime:
        return cached[1]
    raw = _read_raw(path)
    orgs = {_norm(k): _entry_to_org(v or {}) for k, v in (raw.get("agents") or {}).items()}
    _CACHE[str(path)] = (mtime, orgs)
    return orgs


# ── migration ────────────────────────────────────────────────────────────────
def _migrate_into(raw: dict[str, Any]) -> bool:
    """Seed ``raw['agents']`` from each agent.json5 ``org:`` block (missing only).

    Returns True if anything was added. Never overwrites an existing entry.
    """
    try:
        from olano.config.loader import list_agents, load_agent_config
    except Exception:
        return False
    agents = raw.setdefault("agents", {})
    have = {_norm(k) for k in agents}
    changed = False
    for n in list_agents():
        if _norm(n) in have:
            continue
        try:
            org = load_agent_config(n).org
        except Exception:
            continue
        agents[n] = _org_to_entry(org)
        changed = True
    return changed


def _own_org_block(name: str) -> OrgConfig:
    """The ``org`` block from ``agent.json5`` — the pre-migration truth for an
    agent the central store has no entry for yet. Empty on any failure."""
    try:
        from olano.config.loader import load_agent_config

        return load_agent_config(name).org
    except Exception:
        return OrgConfig()


# ── public read API ──────────────────────────────────────────────────────────
def get_org_if_present(name: str, override: str | Path | None = None) -> OrgConfig | None:
    """Central entry for ``name``, or None when the file/entry is absent.

    Used by the runtime policy layer so that, until the store exists, behaviour
    falls back to the booted agent's loaded config (legacy semantics preserved).
    """
    return _load_orgs(override).get(_norm(name))


def get_org(name: str, override: str | Path | None = None) -> OrgConfig:
    """Resolved OrgConfig: central entry if present, else the agent.json5 ``org``
    block (not-yet-migrated), else an empty/unrestricted default."""
    hit = get_org_if_present(name, override)
    if hit is not None:
        return hit
    try:
        from olano.config.loader import load_agent_config

        return load_agent_config(name).org
    except Exception:
        return OrgConfig()


def entry_for(name: str, override: str | Path | None = None) -> dict[str, Any]:
    """The canonical YAML entry dict for ``name`` (for API responses)."""
    return _org_to_entry(get_org(name, override))


def resolve_title(name: str, fallback_org: Any = None, override: str | Path | None = None) -> str:
    """The agent's title (its position — "Recruitment Specialist"), or ``""``.

    ``org.title`` is the one place a title lives, and this is the one place it
    is read: the roster, the detail header, the prompt's identity line,
    ``whoami`` and the peer roster all ask here, so none of them can disagree
    about what an agent's position is. The central ``organization.yaml``
    entry wins when the agent has one (live edits from the Talk to Agent tab
    take effect without a restart); otherwise *fallback_org* — the already
    loaded ``agent.json5`` ``org`` block, as an :class:`OrgConfig` or the raw
    dict a config read hands back — supplies it. Never a disk read of its
    own: callers on the roster path already hold the config.
    """
    central = get_org_if_present(name, override)
    if central is not None:
        return (central.title or "").strip()
    if isinstance(fallback_org, dict):
        title = fallback_org.get("title")
    else:
        title = getattr(fallback_org, "title", None)
    return str(title or "").strip()


# ── cycle detection ──────────────────────────────────────────────────────────
def _would_cycle(
    agents: dict[str, Any], name: str, manager: str | None, cmap: dict[str, str]
) -> bool:
    """True if making ``manager`` the manager of ``name`` closes a reporting loop."""
    seen: set[str] = set()
    cur = manager
    while cur:
        if _norm(cur) == _norm(name):
            return True
        if _norm(cur) in seen:
            break
        seen.add(_norm(cur))
        entry = agents.get(_canon(cur, cmap) or "")
        cur = entry.get("manager") if isinstance(entry, dict) else None
    return False


# ── reciprocity ──────────────────────────────────────────────────────────────
def _apply_reciprocity(
    agents: dict[str, Any],
    name: str,
    old: OrgConfig,
    new: OrgConfig,
    cmap: dict[str, str],
    touched: set[str],
) -> None:
    """Mirror manager/report and co-worker changes onto the counterpart entries.

    Single pass, diff-driven: counterparts are patched terminally (no recursion),
    which guarantees termination. Unknown counterparts (names that aren't real
    agents and have no entry) are recorded one-sided and skipped here.
    """

    def counterpart(n: str) -> tuple[str | None, dict[str, Any] | None]:
        cn = _canon(n, cmap)
        if cn and cn in agents:
            return cn, agents[cn]
        if _norm(n) in cmap:  # a real agent without an entry yet → create one
            cn = cmap[_norm(n)]
            # Seed from the agent's own agent.json5 block, never from a blank
            # OrgConfig: the central entry shadows that block from this moment
            # on, so a blank seed would silently erase the counterpart's
            # title (every archetype install writes one) and its messaging
            # modes the first time anyone names it as a manager or co-worker.
            agents[cn] = _org_to_entry(_own_org_block(cn))
            return cn, agents[cn]
        return None, None  # not an agent we know about — leave one-sided

    old_reports = {_norm(r) for r in old.reports}
    new_reports = {_norm(r) for r in new.reports}

    # Added reports → set their manager to me.
    for r in new.reports:
        if _norm(r) in old_reports:
            continue
        cn, e = counterpart(r)
        if e is not None and _norm(e.get("manager")) != _norm(name):
            e["manager"] = name
            touched.add(cn)  # type: ignore[arg-type]
    # Removed reports → clear their manager if it pointed at me.
    for cn, e in list(agents.items()):
        if _norm(cn) in (old_reports - new_reports) and _norm(e.get("manager")) == _norm(name):
            e["manager"] = None
            touched.add(cn)

    # Manager change → re-parent (remove from old manager's reports, add to new).
    if _norm(new.manager) != _norm(old.manager):
        if old.manager:
            cn, e = counterpart(old.manager)
            if e is not None:
                reps = [x for x in (e.get("reports") or []) if _norm(x) != _norm(name)]
                if len(reps) != len(e.get("reports") or []):
                    e["reports"] = reps
                    touched.add(cn)  # type: ignore[arg-type]
        if new.manager:
            cn, e = counterpart(new.manager)
            if e is not None and not any(_norm(x) == _norm(name) for x in (e.get("reports") or [])):
                e["reports"] = [*(e.get("reports") or []), name]
                touched.add(cn)  # type: ignore[arg-type]

    # Co-workers are mutual.
    old_cow = {_norm(c) for c in old.coworkers}
    new_cow = {_norm(c) for c in new.coworkers}
    for c in new.coworkers:
        if _norm(c) in old_cow:
            continue
        cn, e = counterpart(c)
        if e is not None and not any(_norm(x) == _norm(name) for x in (e.get("coworkers") or [])):
            e["coworkers"] = [*(e.get("coworkers") or []), name]
            touched.add(cn)  # type: ignore[arg-type]
    for c in old.coworkers:
        if _norm(c) not in (old_cow - new_cow):
            continue
        cn, e = counterpart(c)
        if e is not None:
            cows = [x for x in (e.get("coworkers") or []) if _norm(x) != _norm(name)]
            if len(cows) != len(e.get("coworkers") or []):
                e["coworkers"] = cows
                touched.add(cn)  # type: ignore[arg-type]


# ── public write API ─────────────────────────────────────────────────────────
def update_org(
    name: str, new_org: OrgConfig, override: str | Path | None = None
) -> tuple[OrgConfig, set[str]]:
    """Persist ``name``'s org block and reciprocally patch its counterparts.

    Returns ``(canonical_new_org, touched_counterpart_names)``. Raises
    ``ValueError`` if the new manager would create a reporting cycle.
    """
    path = get_olano_paths(override).org_path
    raw = _read_raw(path)
    if not raw.get("agents"):
        # First write — seed from existing agent.json5 blocks so we don't drop
        # other agents' org data on the floor.
        _migrate_into(raw)
    agents = raw.setdefault("agents", {})
    cmap = _canonical_map()
    cname = _canon(name, cmap) or name
    new_org = _canon_org(new_org, cmap)

    if new_org.manager and _would_cycle(agents, cname, new_org.manager, cmap):
        raise ValueError(
            f"Setting manager '{new_org.manager}' for '{cname}' would create a reporting cycle."
        )

    old_org = _entry_to_org(agents.get(cname) or {})
    touched: set[str] = set()
    agents[cname] = _org_to_entry(new_org)
    _apply_reciprocity(agents, cname, old_org, new_org, cmap, touched)
    touched.discard(cname)
    _write_raw(path, raw)
    return new_org, touched
