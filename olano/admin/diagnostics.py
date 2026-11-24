"""Runtime-aware diagnostics — online/offline agents, MCP health, hang detection.

The rest of :mod:`olano.admin` reads from disk and never needs a running
runtime. This module is the exception: it answers operational questions
("which agents are online?", "is this agent hanging?", "are its MCP servers
up?") that only the *live* runtime knows. It degrades gracefully through three
data sources, best first:

1. **In-process** — when AgentFather runs inside ``olano serve`` the live
   :class:`~olano.runtime.server.RuntimeServer` is bound to
   ``olano.tools.talk_to._RUNTIME``. We read ``_runners`` / ``registry`` /
   ``get_mcp_summary`` directly — no HTTP, no auth.
2. **Local API** — from a separate CLI process we probe the daemon's runtime
   API (``http://127.0.0.1:<port>/api/...``) exactly like
   :mod:`olano.runtime.daemon` does.
3. **Disk** — no runtime at all: a config-only snapshot (every agent reported
   offline, MCP servers reported from config).

Every function returns plain JSON-serialisable values and never raises just
because the runtime is down — it reports ``running: false`` instead.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Heuristic: an inbound message / tool call with no follow-up for longer than
# this (while the agent is online) is flagged as a possible hang.
_DEFAULT_STUCK_AFTER_SECONDS = 120
_TAIL_RECORDS = 30


# ── Source resolution ────────────────────────────────────────────────────────


def _get_runtime() -> Any | None:
    """Return the live in-process ``RuntimeServer``, or ``None`` outside serve."""
    try:
        from olano.tools.talk_to import _RUNTIME

        if _RUNTIME is not None and getattr(_RUNTIME, "_runners", None) is not None:
            return _RUNTIME
    except Exception:
        pass
    return None


def _api_get_json(path: str, timeout: float = 2.0) -> Any | None:
    """GET the local runtime API; ``None`` if no daemon is up or it didn't answer.

    Mirrors :func:`olano.runtime.daemon._http_get_json` but adds the bearer
    token when ``LIONCLAW_DASHBOARD_TOKEN`` is set (token-protected deploys).
    """
    import urllib.error
    import urllib.request

    from olano.runtime import daemon

    pid = daemon.read_pid()
    if not (pid and daemon.is_pid_alive(pid)):
        return None
    port = daemon._runtime_api_port()
    headers: dict[str, str] = {}
    token = os.environ.get("LIONCLAW_DASHBOARD_TOKEN", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as exc:
        # An auth rejection is NOT "no daemon" — say so instead of degrading
        # silently, or a deterministic gate reads as a transient outage (on
        # multi-user installs the loopback API wants a signed-in session).
        if exc.code in (401, 403):
            logger.warning(
                "runtime API GET %s refused with HTTP %s (auth gate) — "
                "reporting no data; the loopback API requires a signed-in "
                "session on multi-user installs",
                path,
                exc.code,
            )
        return None
    except (urllib.error.URLError, TimeoutError, ValueError):
        return None


# ── Per-agent status rows (uniform shape across sources) ──────────────────────


def _channels_of(cfg: Any) -> list[str]:
    out: list[str] = []
    for ch in getattr(cfg, "channels", None) or []:
        out.append(ch if isinstance(ch, str) else getattr(ch, "type", "?"))
    return out


def _disk_row(name: str, *, registry: Any = None) -> dict[str, Any]:
    from olano.config.agent_state import is_agent_paused
    from olano.config.loader import load_agent_config

    paused = is_agent_paused(name)
    state = "paused" if paused else "configured"
    error = ""
    model = ""
    channels: list[str] = []
    mcp_count = 0
    try:
        cfg = load_agent_config(name, source="auto")
        model = cfg.model
        channels = _channels_of(cfg)
        mcp_count = len(cfg.mcp_servers or [])
    except Exception as exc:  # malformed config shouldn't hide the agent
        error = str(exc)
        state = "error"
    entry = registry.get(name) if registry is not None else None
    if entry and entry.status == "error" and entry.error:
        state, error = "error", entry.error
    return {
        "name": name,
        "online": False,
        "state": state,
        "paused": paused,
        "error": error,
        "model": model,
        "channels": channels,
        "mcp_count": mcp_count,
    }


def _states_from_runtime(rt: Any) -> list[dict[str, Any]]:
    from olano.config.agent_state import is_agent_paused
    from olano.config.loader import list_agents

    runners = getattr(rt, "_runners", {}) or {}
    registry = getattr(rt, "registry", None)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name, runner in runners.items():
        cfg = getattr(runner, "config", None)
        entry = registry.get(name) if registry is not None else None
        rows.append(
            {
                "name": name,
                "online": True,
                "state": (entry.status if entry and entry.status else "running"),
                "paused": is_agent_paused(name),
                "error": (entry.error if entry else "") or "",
                "model": getattr(cfg, "model", "") if cfg else "",
                "channels": _channels_of(cfg),
                "mcp_count": len(getattr(cfg, "mcp_servers", None) or []) if cfg else 0,
            }
        )
        seen.add(name)
    for name in list_agents():
        if name not in seen:
            rows.append(_disk_row(name, registry=registry))
    return rows


def _states_from_api(payload: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for r in payload:
        online = bool(r.get("running"))
        rows.append(
            {
                "name": r.get("name", ""),
                "online": online,
                "state": r.get("status") or ("running" if online else "configured"),
                "paused": bool(r.get("paused")),
                "error": r.get("error") or "",
                "model": r.get("model", ""),
                "channels": r.get("channels", []) or [],
                "mcp_count": r.get("mcp_count", 0),
            }
        )
    return rows


def _states_from_disk() -> list[dict[str, Any]]:
    from olano.config.loader import list_agents

    return [_disk_row(name) for name in list_agents()]


def _agent_rows() -> tuple[list[dict[str, Any]], str]:
    """Return ``(rows, source)`` — per-agent status from the best available source."""
    rt = _get_runtime()
    if rt is not None:
        return _states_from_runtime(rt), "runtime"
    payload = _api_get_json("/api/agents")
    if isinstance(payload, list):
        return _states_from_api([r for r in payload if isinstance(r, dict)]), "api"
    return _states_from_disk(), "disk"


# ── Public: runtime + agent status ───────────────────────────────────────────


def runtime_status() -> dict[str, Any]:
    """Is the runtime up, and which agents are online vs offline (stopped)?"""
    rows, source = _agent_rows()
    online = sorted(r["name"] for r in rows if r["online"])
    offline = sorted(r["name"] for r in rows if not r["online"])
    out: dict[str, Any] = {
        "source": source,
        "agents_online": online,
        "agents_offline": offline,
        "online_count": len(online),
        "offline_count": len(offline),
    }

    rt = _get_runtime()
    if rt is not None:
        sched = getattr(rt, "scheduler", None)
        out.update(
            {
                "running": True,
                "api_reachable": True,
                "channels": [type(c).__name__ for c in (getattr(rt, "_channels", None) or [])],
                "scheduler_jobs": len(sched.list_jobs()) if sched else 0,
                "detail": "in-process (this agent runs inside the live runtime)",
            }
        )
        return out

    try:
        from olano.runtime import daemon

        st = daemon.status()
        out.update(
            {
                "running": st.running,
                "pid": st.pid,
                "api_reachable": st.api_reachable,
                "api_port": st.api_port,
                "detail": st.detail or ("runtime running" if st.running else "runtime not running"),
            }
        )
        if isinstance(st.status_payload, dict):
            out["channels"] = st.status_payload.get("channels", [])
            out["scheduler_jobs"] = st.status_payload.get("scheduler_jobs", 0)
            out["uptime"] = st.status_payload.get("uptime")
    except Exception as exc:  # pragma: no cover - defensive
        out["running"] = False
        out["detail"] = f"could not probe runtime: {exc}"
    return out


def agent_status(agent: str | None = None) -> dict[str, Any]:
    """Online/offline status for one agent, or split lists for all agents.

    ``online`` means the agent is booted in the live runtime; everything else
    (``configured``, ``paused``, ``stopped``, ``error``) is offline.
    """
    rows, source = _agent_rows()
    if agent is not None:
        match = next((r for r in rows if r["name"] == agent), None)
        if match is None:
            known = sorted(r["name"] for r in rows)
            raise ValueError(f"Unknown agent '{agent}'. Known agents: {known}")
        return {"source": source, **match, "sandbox": sandbox_report(agent)}
    online = [r for r in rows if r["online"]]
    offline = [r for r in rows if not r["online"]]
    return {
        "source": source,
        "online_count": len(online),
        "offline_count": len(offline),
        "online": online,
        "offline": offline,
    }


def sandbox_report(agent: str) -> dict[str, Any]:
    """Whether this agent wants a sandbox container, and whether it has one.

    The gap this closes: an agent with a `lionclaw_docker` backend whose
    container never came up reported ``online: true`` with no boot error, while
    every one of its file and shell tools answered
    "OlanoDockerBackend.start() has not been called". Nothing in `agent_status`
    or `diagnose_agent` mentioned the sandbox, so the only way to find out was
    to read the logs on the box.

    ``configured`` is true when the agent's own backend is a sandbox type
    (its file tools and `execute` run inside the container — ``role``
    ``backend``) OR when the builder started a *sidecar* container beside a
    plain filesystem/virtual backend (``role`` ``sidecar``: the container
    serves the Terminal, the chat ``!`` lane and sandboxed tool subprocesses,
    never the model's tools). Returns ``{"configured": False}`` for an agent
    that never wanted one and never had one started beside it.
    """
    out: dict[str, Any] = {"configured": False}
    backend_type: str | None = None
    configured_by_backend = False
    try:
        from olano.config.backend_config import is_sandbox_backend
        from olano.config.loader import load_agent_config

        cfg = load_agent_config(agent)
        backend = getattr(cfg, "backend", None)
        backend_type = getattr(backend, "type", None)
        configured_by_backend = backend is not None and is_sandbox_backend(backend)
    except Exception as exc:  # unreadable config is its own, separate problem
        logger.debug("sandbox_report: could not read config for %r: %s", agent, exc)
        return out

    try:
        from olano.core.builder import SANDBOX_ROLE_SIDECAR, get_sandbox_backend, sandbox_status

        status = sandbox_status(agent) or {}
        role = status.get("role")
        if not configured_by_backend and role != SANDBOX_ROLE_SIDECAR:
            return out
        live = get_sandbox_backend(agent)
        container = live.container_ref() if live is not None else None
        out.update(
            {
                "configured": True,
                "backend_type": backend_type,
                "role": role or "backend",
                "started": bool(container),
                "container": container,
                "state": status.get("state"),
                "reason": status.get("reason"),
                # What the container's own reachability probe found. On a box
                # nobody can SSH into, this is the only place "the sandbox is
                # up but reaches nothing" is visible — the shape that reads on
                # every other surface as "npx just hangs".
                "network": status.get("network") or {},
            }
        )
    except Exception as exc:
        if not configured_by_backend:
            return out
        out.update(
            {
                "configured": True,
                "backend_type": backend_type,
                "role": "backend",
                "started": False,
                "container": None,
                "state": None,
                "reason": str(exc),
                "network": {},
            }
        )
    return out


# ── Public: MCP status ───────────────────────────────────────────────────────


def _overall(servers: list[dict[str, Any]]) -> str:
    enabled = [s for s in servers if s.get("enabled", True)]
    if not enabled:
        return "none"
    if all(s.get("ok") is True for s in enabled):
        return "green"
    if any(s.get("ok") is not False for s in enabled):
        return "yellow"
    return "red"


def _mcp_from_config(name: str) -> dict[str, Any]:
    from olano.config.loader import load_agent_config

    cfg = load_agent_config(name, source="auto")
    servers = [
        {
            "url": s.url,
            "transport": s.transport,
            "ok": None,
            "status": "offline",
            "enabled": s.enabled,
            "auth": s.auth,
            "tool_count": None,
            "message": "runtime offline — config snapshot only",
        }
        for s in (cfg.mcp_servers or [])
    ]
    return {
        "name": name,
        "running": False,
        "overall": "none" if not servers else "unknown",
        "servers": servers,
        "source": "config",
    }


def _mcp_for_agent(name: str) -> dict[str, Any]:
    rt = _get_runtime()
    if rt is not None:
        running = name in (getattr(rt, "_runners", {}) or {})
        if running:
            try:
                from olano.core.builder import get_mcp_summary

                live = get_mcp_summary(name) or []
            except Exception:
                live = []
            if live:
                servers = [
                    {
                        "url": s.get("url", ""),
                        "transport": s.get("transport", ""),
                        "ok": s.get("ok"),
                        "status": s.get("status", "unknown"),
                        "enabled": s.get("status") != "disabled",
                        "tool_count": len(s.get("tools") or []),
                        "message": s.get("message", ""),
                    }
                    for s in live
                ]
                return {
                    "name": name,
                    "running": True,
                    "overall": _overall(servers),
                    "servers": servers,
                    "source": "runtime",
                }

    health = _api_get_json(f"/api/agents/{name}/mcp/health")
    if isinstance(health, dict) and health.get("ok"):
        return {
            "name": name,
            "running": bool(health.get("running")),
            "overall": health.get("overall", "unknown"),
            "servers": health.get("servers", []),
            "source": "api",
        }

    return _mcp_from_config(name)


def mcp_status(agent: str | None = None) -> dict[str, Any]:
    """Live MCP server health for one agent, or every agent that has MCP servers.

    ``overall`` per agent is ``green`` (all up), ``yellow`` (some up / unknown),
    ``red`` (all down), ``none`` (no enabled servers), or ``unknown`` (offline).
    """
    if agent is not None:
        return _mcp_for_agent(agent)

    from olano.config.loader import list_agents, load_agent_config

    out: list[dict[str, Any]] = []
    for name in list_agents():
        try:
            cfg = load_agent_config(name, source="auto")
        except Exception:
            continue
        if cfg.mcp_servers:
            out.append(_mcp_for_agent(name))
    return {"agents": out, "count": len(out)}


# ── Public: activity / hang detection ────────────────────────────────────────


def _sessions_dir(agent: str) -> Path:
    from olano.config.home import get_olano_paths

    return get_olano_paths().sessions_dir / agent


def _latest_thread_path(agent: str) -> Path | None:
    d = _sessions_dir(agent)
    if not d.exists():
        return None
    files = list(d.glob("*.jsonl"))
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)


def _read_records(path: Path, n: int) -> list[Any]:
    text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out: list[Any] = []
    for ln in text[-n:]:
        try:
            out.append(json.loads(ln))
        except Exception:
            out.append({"raw": ln})
    return out


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def _running_tool(records: list[Any]) -> str | None:
    """Return the name of a tool whose call has no recorded result yet.

    ``tool_call`` / ``tool_result`` are both persisted to the JSONL transcript
    (see ``runner._append_tool_session``). A trailing ``tool_call`` with no
    following ``tool_result`` means that tool is still executing (or hung).
    """
    pending: str | None = None
    for r in records:
        if not isinstance(r, dict):
            continue
        role = r.get("role")
        if role == "tool_call":
            pending = r.get("name") or "tool"
        elif role == "tool_result":
            pending = None
    return pending


def _clip_record(r: Any, limit: int = 300) -> Any:
    if not isinstance(r, dict):
        return r
    out = dict(r)
    content = out.get("content")
    if isinstance(content, str) and len(content) > limit:
        out["content"] = content[:limit] + f"… (+{len(content) - limit} chars)"
    return out


def agent_activity(
    agent: str,
    thread_id: str | None = None,
    *,
    stuck_after_seconds: int = _DEFAULT_STUCK_AFTER_SECONDS,
) -> dict[str, Any]:
    """Is a tool running, or is the agent hanging on its latest thread?

    Inspects the most recently active session transcript (or ``thread_id`` if
    given) and reports whether work is in flight, which tool (if any) has no
    result yet, and — when the agent is online and that work has been pending
    longer than ``stuck_after_seconds`` — flags a likely hang.
    """
    rows, _ = _agent_rows()
    row = next((r for r in rows if r["name"] == agent), None)
    online = bool(row and row["online"])

    path = _sessions_dir(agent) / f"{thread_id}.jsonl" if thread_id else _latest_thread_path(agent)
    if path is None or not path.exists():
        return {
            "agent": agent,
            "online": online,
            "thread_id": thread_id,
            "has_activity": False,
            "in_progress": False,
            "running_tool": None,
            "likely_stuck": False,
            "detail": "no session transcripts found for this agent",
        }

    records = _read_records(path, _TAIL_RECORDS)
    last = records[-1] if records else {}
    last_role = last.get("role") if isinstance(last, dict) else None
    last_ts = _parse_ts(last.get("ts")) if isinstance(last, dict) else None
    age = int((datetime.now(timezone.utc) - last_ts).total_seconds()) if last_ts else None

    running_tool = _running_tool(records)
    awaiting_reply = last_role in ("user", "peer_inbound", "human")
    in_progress = running_tool is not None or awaiting_reply
    likely_stuck = bool(in_progress and online and age is not None and age > stuck_after_seconds)

    if running_tool:
        what = f"tool '{running_tool}' has no recorded result yet"
    elif awaiting_reply:
        what = "an inbound message has no assistant reply yet"
    else:
        what = "idle — last turn completed"

    if likely_stuck:
        detail = f"POSSIBLE HANG: {what} after {age}s while the agent is online."
    elif in_progress and not online:
        detail = f"{what}, but the agent is offline — likely interrupted mid-turn, not hanging."
    elif in_progress:
        suffix = f" ({age}s ago)" if age is not None else ""
        detail = f"In progress: {what}{suffix}."
    else:
        detail = "No work in flight."

    return {
        "agent": agent,
        "online": online,
        "thread_id": path.stem,
        "has_activity": True,
        "last_ts": last.get("ts") if isinstance(last, dict) else None,
        "age_seconds": age,
        "last_role": last_role,
        "running_tool": running_tool,
        "in_progress": in_progress,
        "likely_stuck": likely_stuck,
        "detail": detail,
        "tail": [_clip_record(r) for r in records[-6:]],
    }


# ── Public: one-shot diagnosis ───────────────────────────────────────────────


def diagnose_agent(agent: str) -> dict[str, Any]:
    """Combined health report: state, activity/hang, MCP health, recent errors."""
    from olano.admin.logs import agent_errors

    status = agent_status(agent)
    activity = agent_activity(agent)

    mcp: dict[str, Any] | None = None
    if status.get("mcp_count"):
        try:
            mcp = _mcp_for_agent(agent)
        except Exception as exc:
            mcp = {"error": str(exc)}

    errors = agent_errors(agent, lines=15)
    recent = errors.get("errors", [])
    sandbox = status.get("sandbox") or sandbox_report(agent)

    problems: list[str] = []
    if not status["online"]:
        problems.append(f"agent is offline (state={status['state']})")
    if status.get("error"):
        problems.append(f"boot error: {status['error']}")
    if activity.get("likely_stuck"):
        problems.append(activity["detail"])
    if sandbox.get("configured") and not sandbox.get("started"):
        # The agent is "online" but every file/shell tool it has will fail.
        why = sandbox.get("reason") or "no sandbox container is live"
        problems.append(
            f"backend.type={sandbox.get('backend_type')} but no sandbox container is live: {why}"
        )
    if mcp and mcp.get("overall") == "red":
        problems.append("all enabled MCP servers are unreachable")
    elif mcp and mcp.get("overall") == "yellow":
        problems.append("some MCP servers are degraded")
    if recent:
        problems.append(f"{len(recent)} recent log line(s) mention this agent")

    if problems:
        summary = f"'{agent}': " + "; ".join(problems) + "."
    else:
        summary = f"'{agent}' looks healthy: online, no work stuck, no recent errors."

    return {
        "agent": agent,
        "online": status["online"],
        "state": status["state"],
        "paused": status.get("paused"),
        "error": status.get("error", ""),
        "activity": activity,
        "sandbox": sandbox,
        "mcp": mcp,
        "recent_errors": recent,
        "problems": problems,
        "summary": summary,
    }
