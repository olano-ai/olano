"""Fleet-wide scheduled-job management, across **both** task stores.

Olano surfaces two independent things as "scheduled tasks":

* **DB tasks** — rows in ``<home>/scheduler/scheduler.db`` (``olano.scheduler.db``),
  created by the ``scheduler_*`` agent tools or the dashboard. Addressed by uuid.
* **Config schedules** — ``schedule:`` entries in each agent's ``agent.json5``
  (``olano.config.agent_config.ScheduleConfig``). They never touch the DB and are
  addressed by the synthetic id ``cfg:<agent>:<index>``.

Both must be reachable from one place, or "show me everything scheduled" quietly
means "everything in one of the two stores". The store-fork logic used to live
only inside ``olano/runtime/api.py``'s route handlers; it lives here now and
``api.py`` imports it, so the HTTP surface and AgentFather's ``olano_scheduler_*``
tools can never disagree about what a task is or how to edit one.

**Everything in this module is deliberately fleet-scoped.** Nothing here filters
by ``created_by`` or ``visibility``. That is the point — the callers are the
dashboard (already gated by ``auth_layer``) and the ``owner_only`` admin tools.
The caller-scoped resolver an ordinary agent gets is
``olano.tools.scheduler_tools._resolve_task_id``, which is a different function
on purpose; do not route fleet calls through it or route these through it.
"""

from __future__ import annotations

import logging
from typing import Any

from olano.scheduler.db import SchedulerDB

logger = logging.getLogger(__name__)

CONFIG_JOB_PREFIX = "cfg:"

#: Fields a config-schedule entry understands. A DB task has more (visibility,
#: run_at, agent reassignment); asking for those on a ``cfg:`` id is an error
#: rather than a silent no-op, so a caller never believes a write landed.
_CONFIG_ONLY_FIELDS = (
    "name",
    "prompt",
    "schedule",
    "timezone",
    "enabled",
    "model",
    "reasoning_effort",
)


def validate_task_model(model: str | None) -> str:
    """Normalize a task's ``model`` and refuse one this box cannot run.

    Returns ``""`` for "the agent's configured model" (None, empty, blanks),
    else the trimmed ``provider:model`` string. A model whose provider is not
    connected raises ``ValueError`` naming why — the same predicate
    ``self_set_model`` applies — so a task can never be saved on a model that
    would fail every run. Every surface that writes a task model (routes, the
    admin tools, the agent's own ``scheduler_*`` tools) goes through here.
    """
    value = str(model or "").strip()
    if not value:
        return ""
    from olano.admin.models import model_provider_connected

    connected, why = model_provider_connected(value)
    if not connected:
        raise ValueError(f"model '{value}' is not usable here: {why}")
    return value


def agent_configured_model(agent_name: str | None) -> str | None:
    """The model a job runs on when it names none: the owning agent's own.

    An unnamed agent means the deployment's default agent (what the scheduler
    itself falls back to at fire time). Unreadable ⇒ ``None``, and the effort
    validator then refuses an effort with no model to check it against.
    """
    from olano.config.home import load_home_config
    from olano.config.loader import load_agent_config

    name = (agent_name or "").strip() or load_home_config().default_agent
    if not name:
        return None
    try:
        return load_agent_config(name).model
    except Exception:
        return None


def validate_task_effort(
    reasoning_effort: str | None, *, model: str, agent_name: str | None
) -> str:
    """Normalize a task's ``reasoning_effort`` against the model it will run on.

    ``model`` is the task's own (validated, ``""`` = none) and ``agent_name``
    the agent whose configured model applies when it has none. Returns ``""``
    for "the agent's own setting", else the lower-cased level; raises
    ``ValueError`` naming the ladder the model accepts. Every surface that
    writes a task effort goes through here, next to :func:`validate_task_model`.
    """
    from olano.scheduler.overrides import validate_task_effort as _validate

    runs_on = model or agent_configured_model(agent_name)
    return _validate(reasoning_effort, runs_on=runs_on)


# ── id shapes ─────────────────────────────────────────────────────────────────


def config_job_id(agent: str, index: int) -> str:
    """The synthetic id for one ``agent.json5`` schedule entry."""
    return f"{CONFIG_JOB_PREFIX}{agent}:{index}"


def is_config_job_id(task_id: str) -> bool:
    return task_id.startswith(CONFIG_JOB_PREFIX)


def parse_config_job_id(task_id: str) -> tuple[str, int]:
    """Split ``cfg:{agent}:{index}`` → (agent, index).

    ``rpartition`` tolerates a colon inside an agent name. Raises ``ValueError``
    on a malformed id — ``runtime/api.py`` translates that to its 404.
    """
    body = task_id[len(CONFIG_JOB_PREFIX) :]
    agent, sep, idx = body.rpartition(":")
    if not sep or not agent or not idx.isdigit():
        raise ValueError(f"Invalid config job id '{task_id}'")
    return agent, int(idx)


def entry_cron_string(entry: dict) -> str:
    """5-field cron string for a raw agent.json5 schedule entry. ``cron`` wins when
    it's a valid 5-field expression (mirrors the scheduler's _make_trigger)."""
    cron = str(entry.get("cron") or "").strip()
    if cron and len(cron.split()) == 5:
        return cron
    return "{} {} {} {} {}".format(
        entry.get("minute", "*"),
        entry.get("hour", "*"),
        entry.get("day", "*"),
        entry.get("month", "*"),
        entry.get("day_of_week", "*"),
    )


# ── config-schedule store (agent.json5) ───────────────────────────────────────


def config_schedule_tasks(agent: str | None = None) -> list[dict]:
    """Every agent.json5 ``schedule`` entry, mapped into the Task shape and
    tagged ``source='config'``.

    Source of truth is each agent's file (so entries for agents that aren't
    currently booted still show up — the whole point is no hidden jobs); the live
    ``next_run`` and registration status come from the active scheduler when the
    runtime is running.

    ``agent`` limits the walk to one agent, so a scoped list doesn't JSON5-parse
    every config on disk.
    """
    from olano.config.loader import list_agents
    from olano.runtime.api import _read_agent_config
    from olano.scheduler.cron import get_active_scheduler

    live_by_id: dict[str, dict] = {}
    sched = get_active_scheduler()
    if sched is not None:
        try:
            live_by_id = {j["id"]: j for j in sched.list_config_jobs(agent)}
        except Exception:
            live_by_id = {}

    db = SchedulerDB()
    names = [agent] if agent else list(list_agents())
    out: list[dict] = []
    for name in names:
        try:
            data = _read_agent_config(name)
        except Exception:
            continue
        entries = data.get("schedule")
        if not isinstance(entries, list):
            continue
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                continue
            job_id = config_job_id(name, index)
            message = str(entry.get("message") or "")
            label = str(entry.get("name") or "").strip() or (
                message[:60] or f"{name} schedule {index + 1}"
            )
            stats = db.history_stats(job_id)
            live = live_by_id.get(job_id)
            out.append(
                {
                    "id": job_id,
                    "name": label,
                    "prompt": message,
                    "schedule": entry_cron_string(entry),
                    "run_at": None,
                    "timezone": entry.get("timezone") or "UTC",
                    "enabled": bool(entry.get("enabled", True)),
                    "agent_name": name,
                    "created_at": "",
                    "last_run": stats["last_run"],
                    "next_run": (live or {}).get("next_run"),
                    "run_count": stats["run_count"],
                    "visibility": "shared",
                    "created_by": name,
                    "source": "config",
                    "index": index,
                    "live": live is not None,
                    # The entry's own model / effort ("" = the agent's own),
                    # same keys and spelling as a DB task row.
                    "model": str(entry.get("model") or "").strip(),
                    "reasoning_effort": str(entry.get("reasoning_effort") or "").strip().lower(),
                }
            )
    return out


def write_and_resync_schedule(agent: str, data: dict) -> None:
    """Validate the mutated agent config, write agent.json5, and live-reregister
    just that agent's config-schedule jobs (heartbeat/cognition untouched).

    The write goes through ``runtime/api._write_agent_config`` — imported lazily
    to keep this module import-cycle free — because that is where the deployment
    limit guard and the channel/MCP secret absorption live. ``agent.json5`` has
    exactly one write path and this is not a second one.
    """
    from olano.config.agent_config import AgentConfig

    try:
        merged = AgentConfig(**data)
    except Exception as exc:
        raise ValueError(f"Invalid agent config: {exc}") from exc

    from olano.runtime.api import _write_agent_config

    _write_agent_config(agent, data)
    try:
        from olano.scheduler.cron import get_active_scheduler

        sched = get_active_scheduler()
        if sched is not None:
            sched.resync_agent_config_schedule(merged)
    except Exception as exc:
        logger.debug("Schedule resync skipped for '%s': %s", agent, exc)


def _load_entries(agent: str) -> tuple[dict, list]:
    """The agent's config data plus its ``schedule`` list (always a list)."""
    from olano.runtime.api import _read_agent_config

    try:
        data = _read_agent_config(agent)
    except FileNotFoundError as exc:
        raise ValueError(f"Agent '{agent}' not found") from exc
    entries = data.get("schedule")
    if not isinstance(entries, list):
        entries = []
    return data, entries


def read_config_entry(agent: str, index: int) -> dict:
    """One raw ``agent.json5`` schedule entry."""
    _data, entries = _load_entries(agent)
    if index >= len(entries) or index < 0:
        raise ValueError(f"Config job {config_job_id(agent, index)} not found")
    entry = entries[index]
    if not isinstance(entry, dict):
        raise ValueError(f"Malformed schedule entry {config_job_id(agent, index)}")
    return entry


def apply_config_entry_update(
    agent: str,
    index: int,
    *,
    name: str | None = None,
    message: str | None = None,
    cron: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> list[str]:
    """Patch one schedule entry in place. Returns the field names that changed.

    ``model=""`` / ``reasoning_effort=""`` clear the entry's own value (back to
    the agent's); ``None`` keeps it. The model is validated by
    :func:`validate_task_model`; the effort by :func:`validate_task_effort`
    against the pair *as it will be after the edit*, so a model change can
    never leave an effort the new model rejects on the entry.
    """
    data, entries = _load_entries(agent)
    if index >= len(entries) or index < 0:
        raise ValueError(f"Config job {config_job_id(agent, index)} not found")
    entry = entries[index]
    if not isinstance(entry, dict):
        raise ValueError(f"Malformed schedule entry {config_job_id(agent, index)}")

    changed: list[str] = []
    if name is not None:
        entry["name"] = name
        changed.append("name")
    if message is not None:
        entry["message"] = message
        changed.append("prompt")
    if cron is not None and cron.strip():
        # Canonicalize onto `cron` and drop the split minute/hour/… fields so
        # there's a single source of truth (and no every-minute trap if someone
        # later clears `cron`). An empty value is ignored rather than blanking
        # the schedule into an all-"*" every-minute job.
        entry["cron"] = cron.strip()
        for stale in ("minute", "hour", "day", "month", "day_of_week"):
            entry.pop(stale, None)
        changed.append("schedule")
    if timezone is not None:
        entry["timezone"] = timezone
        changed.append("timezone")
    if enabled is not None:
        entry["enabled"] = bool(enabled)
        changed.append("enabled")
    if model is not None or reasoning_effort is not None:
        next_model = (
            validate_task_model(model)
            if model is not None
            else str(entry.get("model") or "").strip()
        )
        next_effort = validate_task_effort(
            entry.get("reasoning_effort") if reasoning_effort is None else reasoning_effort,
            model=next_model,
            agent_name=agent,
        )
        # Back to the agent's own value: drop the key rather than writing an
        # empty string into agent.json5.
        for key, value, touched in (
            ("model", next_model, model is not None),
            ("reasoning_effort", next_effort, reasoning_effort is not None),
        ):
            if value:
                entry[key] = value
            else:
                entry.pop(key, None)
            if touched:
                changed.append(key)

    data["schedule"] = entries
    write_and_resync_schedule(agent, data)
    return changed


def append_config_entry(
    agent: str,
    *,
    name: str,
    message: str,
    cron: str,
    timezone: str | None = None,
    enabled: bool = True,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> str:
    """Add a schedule entry to ``agent.json5``. Returns its new ``cfg:`` id."""
    if not cron.strip():
        raise ValueError("A config schedule needs a cron expression.")
    validated_model = validate_task_model(model)
    validated_effort = validate_task_effort(
        reasoning_effort, model=validated_model, agent_name=agent
    )
    data, entries = _load_entries(agent)
    entry: dict[str, Any] = {
        "name": name,
        "message": message,
        "cron": cron.strip(),
        "enabled": bool(enabled),
    }
    if timezone:
        entry["timezone"] = timezone
    if validated_model:
        entry["model"] = validated_model
    if validated_effort:
        entry["reasoning_effort"] = validated_effort
    entries.append(entry)
    data["schedule"] = entries
    write_and_resync_schedule(agent, data)
    return config_job_id(agent, len(entries) - 1)


def delete_config_entry(agent: str, index: int) -> None:
    """Remove one schedule entry from ``agent.json5``.

    Note the ids of later entries shift down by one — they are positional, which
    is why callers should re-list rather than reuse a held id after a delete.
    """
    data, entries = _load_entries(agent)
    if index >= len(entries) or index < 0:
        raise ValueError(f"Config job {config_job_id(agent, index)} not found")
    entries.pop(index)
    data["schedule"] = entries
    write_and_resync_schedule(agent, data)


# ── fleet service layer ───────────────────────────────────────────────────────


def _require_agent(name: str) -> None:
    from olano.admin.lifecycle import require_agent

    require_agent(name)


def _notify(action: str, task_id: str) -> None:
    """Best-effort dashboard SSE so the Scheduler view and the active-jobs badge
    reflect a fleet-tool change immediately. Never let a dead socket fail a write."""
    try:
        from olano.runtime.api import _push_event

        _push_event("scheduler_update", {"action": action, "id": task_id})
    except Exception:
        pass


def _sync_db_task(task_id: str) -> str:
    """Re-register a DB task with the live scheduler. Returns a status line."""
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is None:
            return "Scheduler is not active; the job applies when the runtime restarts."
        _ok, message = scheduler.sync_db_task(task_id)
        return str(message)
    except Exception as exc:
        return f"Scheduler sync skipped: {exc}"


def resolve_task_ref(task_id: str) -> tuple[str, str]:
    """Resolve a task reference to ``(source, full_id)``.

    ``cfg:`` ids pass through verbatim. A DB reference may be a full uuid or a
    prefix (the 8-char form every listing shows), matched **fleet-wide** — that
    is the whole point of this module, and the reason it does not reuse
    ``scheduler_tools._resolve_task_id``, which is caller-scoped. An ambiguous
    prefix raises rather than picking the first match.
    """
    ref = task_id.strip()
    if not ref:
        raise ValueError("A task id is required.")
    if is_config_job_id(ref):
        parse_config_job_id(ref)  # validate shape now, not at the write
        return "config", ref

    tasks = SchedulerDB().list_tasks()
    exact = [t for t in tasks if t["id"] == ref]
    if exact:
        return "db", str(exact[0]["id"])
    matches = [t for t in tasks if str(t["id"]).startswith(ref)]
    if not matches:
        raise ValueError(f"No scheduled task found with id starting with '{ref}'.")
    if len(matches) > 1:
        names = ", ".join(f"{t['id'][:12]} ({t['name']})" for t in matches[:5])
        raise ValueError(f"Ambiguous task id '{ref}' — matches {len(matches)}: {names}")
    return "db", str(matches[0]["id"])


def _preview(record: dict, chars: int = 200) -> dict:
    """A list row: the prompt replaced by a bounded preview plus its true length.

    A fleet of 60 jobs must not dump 60 full prompts into one turn, and a reader
    that cannot tell a preview from a whole prompt is exactly the defect this
    module exists to fix — hence ``prompt_chars`` alongside.
    """
    prompt = str(record.get("prompt") or "")
    row = {k: v for k, v in record.items() if k != "prompt"}
    row["prompt_preview"] = prompt[:chars]
    row["prompt_chars"] = len(prompt)
    row["prompt_truncated"] = len(prompt) > chars
    return row


def list_tasks(agent: str = "", source: str = "") -> dict:
    """Every scheduled job in the deployment, from both stores.

    ``agent`` limits to one agent; ``source`` to ``"db"`` or ``"config"``.
    Prompts come back as bounded previews — use :func:`get_task` for the full text.
    """
    if agent:
        _require_agent(agent)
    if source and source not in ("db", "config"):
        raise ValueError(f"source must be 'db' or 'config', got {source!r}")

    from olano.scheduler.cron import get_active_scheduler

    db_rows: list[dict] = []
    if source != "config":
        for row in SchedulerDB().list_tasks(agent_name=agent or None):
            row["source"] = "db"
            db_rows.append(row)
    cfg_rows = [] if source == "db" else config_schedule_tasks(agent or None)

    return {
        "count": len(db_rows) + len(cfg_rows),
        "db_count": len(db_rows),
        "config_count": len(cfg_rows),
        "scheduler_active": get_active_scheduler() is not None,
        "agent": agent or None,
        "tasks": [_preview(r) for r in db_rows + cfg_rows],
    }


def get_task(task_id: str, *, history: int = 5) -> dict:
    """One job in full — including its **complete, untruncated** prompt.

    This is the read that ``scheduler_list``/``scheduler_search`` cannot do: they
    show a name and at most a 100-char preview, so editing a prompt through them
    is a blind overwrite. History results/errors are also untruncated, because
    reading a failed run's full error is the point of asking.
    """
    source, full_id = resolve_task_ref(task_id)
    db = SchedulerDB()

    if source == "config":
        agent, index = parse_config_job_id(full_id)
        rows = [t for t in config_schedule_tasks(agent) if t["id"] == full_id]
        if not rows:
            raise ValueError(f"Config job {full_id} not found")
        record = dict(rows[0])
    else:
        task = db.get_task(full_id)
        if task is None:
            raise ValueError(f"Task {full_id} not found")
        record = dict(task)
        record["source"] = "db"

    from olano.scheduler.cron import get_active_scheduler

    scheduler = get_active_scheduler()
    registered = False
    if scheduler is not None:
        try:
            if source == "config":
                registered = bool(record.get("live"))
            else:
                # cron._db_job_id: a DB task registers as "db_<first 8 of uuid>".
                job_id = f"db_{full_id[:8]}"
                registered = any(j.get("id") == job_id for j in scheduler.list_jobs())
        except Exception:
            registered = False

    record["history"] = db.get_history(full_id, limit=max(0, min(int(history), 50)))
    record["runtime"] = {
        "scheduler_active": scheduler is not None,
        "registered": registered,
        "next_run": record.get("next_run"),
    }
    return record


def create_task(
    agent: str,
    name: str,
    prompt: str,
    *,
    schedule: str = "",
    run_at: str = "",
    timezone: str = "UTC",
    store: str = "db",
    visibility: str = "shared",
    model: str = "",
    reasoning_effort: str = "",
) -> dict:
    """Schedule a new job for ``agent``.

    ``store="db"`` writes a scheduler-database task (the default; supports
    one-time ``run_at`` and later reassignment). ``store="config"`` writes a
    ``schedule:`` entry into the agent's ``agent.json5`` — durable, visible in
    the config, and what long-standing routines usually want. ``model`` pins
    the job to one ``provider:model``; empty keeps the agent's configured one.
    ``reasoning_effort`` is one of the levels the model the job runs on
    accepts; empty keeps the agent's own setting.
    """
    _require_agent(agent)
    if not name.strip():
        raise ValueError("A task needs a name.")
    if not prompt.strip():
        raise ValueError("A task needs a prompt — it is what the agent will be sent.")
    if store not in ("db", "config"):
        raise ValueError(f"store must be 'db' or 'config', got {store!r}")
    if not schedule.strip() and not run_at.strip():
        raise ValueError("Provide either a cron `schedule` or a one-time `run_at`.")
    task_model = validate_task_model(model)
    task_effort = validate_task_effort(reasoning_effort, model=task_model, agent_name=agent)

    if store == "config":
        if run_at.strip():
            raise ValueError("A config schedule is cron-only; use store='db' for a one-time run.")
        task_id = append_config_entry(
            agent,
            name=name.strip(),
            message=prompt,
            cron=schedule,
            timezone=timezone or None,
            model=task_model,
            reasoning_effort=task_effort,
        )
        _notify("created", task_id)
        return {
            "ok": True,
            "id": task_id,
            "source": "config",
            "agent": agent,
            "runtime": "Registered from agent.json5.",
        }

    embedding = None
    try:
        from olano.memory.embeddings import embed_text

        embedding = embed_text(f"{name}\n{prompt}")
    except Exception:
        embedding = None

    task_id = SchedulerDB().create_task(
        name=name.strip(),
        prompt=prompt,
        schedule=schedule.strip() or None,
        run_at=run_at.strip() or None,
        timezone=timezone or "UTC",
        agent_name=agent,
        embedding=embedding,
        visibility=visibility if visibility in ("private", "shared") else "shared",
        created_by=agent,
        model=task_model or None,
        reasoning_effort=task_effort or None,
    )
    runtime = _sync_db_task(task_id)
    _notify("created", task_id)
    return {"ok": True, "id": task_id, "source": "db", "agent": agent, "runtime": runtime}


def update_task(
    task_id: str,
    *,
    name: str | None = None,
    prompt: str | None = None,
    schedule: str | None = None,
    run_at: str | None = None,
    timezone: str | None = None,
    enabled: bool | None = None,
    visibility: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
) -> dict:
    """Patch a scheduled job. ``None`` means *keep* — matching the HTTP layer.

    Unlike the ordinary ``scheduler_update`` tool (where ``""`` means keep and a
    field can therefore never be cleared), an empty string here means **clear**
    for the fields where that is meaningful: ``run_at=""`` drops a one-time
    fire, ``agent=""`` falls back to the deployment's default agent,
    ``model=""`` returns the job to the agent's configured model and
    ``reasoning_effort=""`` to the agent's own effort. An empty ``name`` or
    ``prompt`` is refused rather than written — the DB would happily persist
    an empty prompt, and no job wants one.
    """
    source, full_id = resolve_task_ref(task_id)
    if name is not None and not name.strip():
        raise ValueError("name cannot be blank — omit it to keep the current value.")
    if prompt is not None and not prompt.strip():
        raise ValueError("prompt cannot be blank — omit it to keep the current value.")

    if source == "config":
        unsupported = [
            field
            for field, value in (("run_at", run_at), ("visibility", visibility), ("agent", agent))
            if value is not None
        ]
        if unsupported:
            raise ValueError(
                f"{', '.join(unsupported)} cannot be set on a config-schedule job "
                f"({full_id}); those apply to scheduler-database tasks only."
            )
        cfg_agent, index = parse_config_job_id(full_id)
        changed = apply_config_entry_update(
            cfg_agent,
            index,
            name=name,
            message=prompt,
            cron=schedule,
            timezone=timezone,
            enabled=enabled,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if not changed:
            raise ValueError(
                f"No fields to update. Supported here: {', '.join(_CONFIG_ONLY_FIELDS)}."
            )
        _notify("updated", full_id)
        return {
            "ok": True,
            "id": full_id,
            "source": "config",
            "agent": cfg_agent,
            "changed": changed,
            "runtime": "agent.json5 rewritten and reregistered.",
        }

    if agent:
        _require_agent(agent)
    updates: dict[str, Any] = {}
    if name is not None:
        updates["name"] = name.strip()
    if prompt is not None:
        updates["prompt"] = prompt
    if schedule is not None:
        updates["schedule"] = schedule.strip() or None
    if run_at is not None:
        updates["run_at"] = run_at.strip() or None
    if timezone is not None:
        updates["timezone"] = timezone
    if enabled is not None:
        updates["enabled"] = 1 if enabled else 0
    if visibility is not None:
        if visibility not in ("private", "shared"):
            raise ValueError("visibility must be 'private' or 'shared'.")
        updates["visibility"] = visibility
    if agent is not None:
        updates["agent_name"] = agent.strip() or None
    if model is not None or reasoning_effort is not None:
        # Validated as the pair will be after the edit: a new model is checked
        # against the stored effort, a new effort against the stored model.
        current = SchedulerDB().get_task(full_id) or {}
        next_model = (
            validate_task_model(model)
            if model is not None
            else str(current.get("model") or "").strip()
        )
        next_effort = validate_task_effort(
            current.get("reasoning_effort") if reasoning_effort is None else reasoning_effort,
            model=next_model,
            agent_name=(agent if agent is not None else current.get("agent_name")) or None,
        )
        if model is not None:
            updates["model"] = next_model or None
        if reasoning_effort is not None:
            updates["reasoning_effort"] = next_effort or None
    if not updates:
        raise ValueError("No fields to update.")

    if not SchedulerDB().update_task(full_id, **updates):
        raise ValueError(f"Task {full_id} not found")
    runtime = _sync_db_task(full_id)
    _notify("updated", full_id)
    owner = SchedulerDB().get_task(full_id) or {}
    return {
        "ok": True,
        "id": full_id,
        "source": "db",
        "agent": owner.get("agent_name"),
        "changed": sorted(updates),
        "runtime": runtime,
    }


def delete_task(task_id: str) -> dict:
    """Permanently delete a scheduled job from whichever store holds it."""
    source, full_id = resolve_task_ref(task_id)
    if source == "config":
        agent, index = parse_config_job_id(full_id)
        delete_config_entry(agent, index)
        _notify("deleted", full_id)
        return {
            "ok": True,
            "id": full_id,
            "source": "config",
            "agent": agent,
            "note": "Later cfg: ids for this agent shift down by one — re-list before reusing one.",
        }

    task = SchedulerDB().get_task(full_id) or {}
    if not SchedulerDB().delete_task(full_id):
        raise ValueError(f"Task {full_id} not found")
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            scheduler.remove_db_task(full_id)
    except Exception:
        pass
    _notify("deleted", full_id)
    return {"ok": True, "id": full_id, "source": "db", "agent": task.get("agent_name")}


def run_task_now(task_id: str) -> dict:
    """Fire a job immediately, ignoring its schedule.

    Needs the runtime scheduler live (and, for a config job, the owning agent
    booted). This makes the owning agent take a full turn now.
    """
    from olano.scheduler.cron import get_active_scheduler

    source, full_id = resolve_task_ref(task_id)
    scheduler = get_active_scheduler()
    if scheduler is None:
        raise ValueError("Scheduler is not active in this runtime.")

    if source == "config":
        ok, message = scheduler.run_config_job_now(full_id)
    else:
        if SchedulerDB().get_task(full_id) is None:
            raise ValueError(f"Task {full_id} not found")
        ok, message = scheduler.run_db_task_now(full_id)
    if not ok:
        raise ValueError(str(message))
    _notify("ran", full_id)
    return {"ok": True, "id": full_id, "source": source, "message": str(message)}
