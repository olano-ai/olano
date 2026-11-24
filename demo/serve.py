#!/usr/bin/env python3
"""Boot a throwaway dashboard for one demo video and keep it running.

    uv run python demo/serve.py tour

Uses the launcher the browser tests use (``tests/e2e/harness.py``): the real
runtime API and the real React app from source, in a temporary home. The
demo's fictional team comes from ``demo/teams/<demo>.py`` and its agents
answer from a script, so no model is called and no key is read.

Prints one JSON line (``{"base_url": ..., "runtime_url": ..., "home": ...}``)
once the dashboard is ready, then waits until it is killed. ``record.mjs``
starts it and stops it for every take.

The page in the recording runs ``DEMO_SLOW`` times slower than real time, so
every pause in a scripted turn is stretched by the same factor.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import signal
import sys
import threading
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

SLOW = float(os.environ.get("DEMO_SLOW", "1"))

# Set once the dashboard is up; scripted turns use them to act on the runtime.
PUSH_EVENT: Callable[[str, dict], None] | None = None
RUNTIME_URL = ""
LOOP: asyncio.AbstractEventLoop | None = None  # the runtime's event loop
GATEWAY: Any = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _message_text(message: Any) -> str:
    if isinstance(message, str):
        return message
    if isinstance(message, list):
        return " ".join(str(p.get("text", "")) for p in message if isinstance(p, dict))
    return str(getattr(message, "content", message))


class ScriptedRunner:
    """Plays a written turn: tool steps, pauses, text, an approval, a hand-off.

    Yields the same ``(kind, payload)`` events the real runner does, and writes
    the same transcript rows, so the dashboard draws and reloads the turn the
    way it would a real one.
    """

    def __init__(self, config: Any, turns: list[dict[str, Any]]) -> None:
        self.config = config
        self._turns = turns
        self.last_trace_id = None

    def _pick(self, text: str) -> dict[str, Any]:
        low = text.lower()
        for turn in self._turns:
            if turn.get("match") and turn["match"].lower() in low:
                return turn
        for turn in self._turns:
            if not turn.get("match"):
                return turn
        return {"steps": [{"text": "Noted. I will get back to you shortly."}]}

    def _persist(self, thread_id: str, row: dict[str, Any]) -> None:
        home = os.environ.get("LIONCLAW_HOME")
        if not home or not thread_id:
            return
        sessions = Path(home) / "sessions" / self.config.name
        sessions.mkdir(parents=True, exist_ok=True)
        with (sessions / f"{thread_id}.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": _now(), **row}, ensure_ascii=False) + "\n")

    async def ainvoke(self, message: Any, thread_id: str | None = None, **kw: Any) -> str:
        parts = []
        async for kind, payload in self.astream_events(message, thread_id=thread_id, **kw):
            if kind == "text":
                parts.append(payload)
        return "".join(parts)

    def record_turn_error(self, *_: Any, **__: Any) -> None:
        return None

    async def astream_events(
        self,
        message: Any,
        thread_id: str | None = None,
        approval_handler: Any = None,
        **_: Any,
    ):
        text = _message_text(message)
        turn = self._pick(text)
        tid = thread_id or ""
        channel = "scheduler" if tid.startswith("scheduled_") else "api"
        self._persist(tid, {"role": "user", "content": text, "channel": channel})
        workspace = Path(str(self.config.workspace))
        said: list[str] = []
        for step in turn["steps"]:
            if "wait" in step:
                await asyncio.sleep(step["wait"] * SLOW)
            elif "tool" in step:
                call_id = "call_" + uuid.uuid4().hex[:10]
                args = json.dumps(step.get("args", {}), ensure_ascii=False)
                yield ("tool_call", {"id": call_id, "name": step["tool"], "args": args})
                self._persist(
                    tid,
                    {
                        "role": "tool_call",
                        "tool_call_id": call_id,
                        "name": step["tool"],
                        "args": args,
                    },
                )
                if "peer" in step and PUSH_EVENT is not None:
                    peer = step["peer"]
                    base = {
                        "kind": "peer",
                        "from": self.config.name,
                        "to": peer["to"],
                        "message": peer["message"],
                    }
                    sent = _now()
                    PUSH_EVENT("agent_message", {**base, "phase": "send", "ts": sent})
                await asyncio.sleep(step.get("took", 1.0) * SLOW)
                if "write" in step:
                    for rel, body in step["write"].items():
                        path = workspace / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        if isinstance(body, bytes):
                            path.write_bytes(body)
                        else:
                            path.write_text(body, encoding="utf-8")
                if "peer" in step and PUSH_EVENT is not None:
                    PUSH_EVENT(
                        "agent_message",
                        {
                            **base,
                            "phase": "reply",
                            "reply": peer["reply"],
                            "ts_sent": sent,
                            "ts_reply": _now(),
                        },
                    )
                result = step.get("result", "ok")
                if callable(result):  # the product's own tool, run for real (off the loop)
                    result = await asyncio.to_thread(
                        result, {"agent": self.config.name, "message": text, "thread_id": tid}
                    )
                yield ("tool_result", {"id": call_id, "name": step["tool"], "content": result})
                self._persist(
                    tid,
                    {
                        "role": "tool_result",
                        "tool_call_id": call_id,
                        "name": step["tool"],
                        "content": result,
                    },
                )
            elif "approve" in step:
                actions = step["approve"]
                yield ("approval", {"actions": actions})
                if approval_handler is not None:
                    await approval_handler(actions)
            elif "text" in step:
                # Words per second, at the page's pace.
                gap = SLOW / float(step.get("wps", 14))
                body = step["text"]
                if callable(body):  # text the product computes (a real tool's output)
                    body = await asyncio.to_thread(
                        body, {"agent": self.config.name, "message": text, "thread_id": tid}
                    )
                words = body.split(" ")
                for i, word in enumerate(words):
                    chunk = word + (" " if i < len(words) - 1 else "")
                    said.append(chunk)
                    yield ("text", chunk)
                    await asyncio.sleep(gap)
        self._persist(tid, {"role": "assistant", "content": "".join(said), "channel": channel})


def _next_run(task_id: str) -> str | None:
    """The next fire time of a saved task, from its own cron line."""
    from apscheduler.triggers.cron import CronTrigger

    from olano.scheduler import clock
    from olano.scheduler.db import SchedulerDB

    task = next((t for t in SchedulerDB().list_tasks() if str(t.get("id")) == str(task_id)), None)
    if not task or not task.get("schedule"):
        return None
    try:
        minute, hour, day, month, dow = str(task["schedule"]).split()
        trigger = CronTrigger(
            minute=minute,
            hour=hour,
            day=day,
            month=month,
            day_of_week=clock.standard_weekdays(dow),
            timezone=task.get("timezone") or "UTC",
        )
        nxt = trigger.get_next_fire_time(None, datetime.now(timezone.utc))
    except Exception:
        return None
    return nxt.isoformat() if nxt else None


class StandInScheduler:
    """What the Scheduler tab asks the live scheduler for; nothing here fires jobs."""

    def next_run_for_task(self, task_id: str) -> str | None:
        return _next_run(task_id)

    def run_db_task_now(self, task_id: str) -> tuple[bool, str]:
        """Run now: the task's agent answers its prompt on a fresh scheduled thread."""
        from olano.scheduler.cron import fresh_run_thread_id
        from olano.scheduler.db import SchedulerDB

        task = SchedulerDB().get_task(task_id)
        if task is None or LOOP is None or GATEWAY is None:
            return False, f"Task {task_id[:8]} not found."
        agent = task.get("agent_name") or ""
        runner = GATEWAY._runners.get(agent)
        if runner is None:
            return False, f"Agent {agent} is not running."
        thread_id = fresh_run_thread_id(f"scheduled_{agent}_{task_id[:8]}")

        async def _go() -> None:
            if PUSH_EVENT is not None:
                PUSH_EVENT("agent_activity", {"agent": agent, "status": "thinking"})
            result = await runner.ainvoke(task.get("prompt") or "", thread_id=thread_id)
            SchedulerDB().add_history(task_id, result=result[:2000])
            if PUSH_EVENT is not None:
                PUSH_EVENT("agent_activity", {"agent": agent, "status": "idle"})
                PUSH_EVENT("scheduler_updated", {"task_id": task_id})

        asyncio.run_coroutine_threadsafe(_go(), LOOP)
        return True, f"Task {task_id[:8]} queued for immediate execution."

    def __getattr__(self, name: str) -> Any:
        return lambda *a, **k: None


def build_gateway(team: dict[str, Any]) -> Any:
    from harness import _gateway_for

    from olano.config.agent_config import AgentConfig, OrgConfig
    from olano.config.backend_config import FilesystemBackendConfig

    runners: dict[str, Any] = {}
    for spec in team["agents"]:
        cfg = AgentConfig(
            name=spec["name"],
            model=spec.get("model", "olano:pro"),
            backend=FilesystemBackendConfig(root_dir="workspace"),
        )
        cfg.emoji = spec.get("emoji")
        cfg.description = spec.get("description", "")
        if spec.get("display_name"):
            cfg.display_name = spec["display_name"]
        cfg.channels = []
        cfg.org = OrgConfig(
            title=spec.get("title"),
            manager=spec.get("manager"),
            reports=list(spec.get("reports", [])),
        )
        runners[spec["name"]] = ScriptedRunner(cfg, team.get("turns", {}).get(spec["name"], []))
    import olano.scheduler.cron as cron

    cron.get_active_scheduler = lambda: StandInScheduler()  # type: ignore[assignment]
    return _gateway_for(runners)


def seed_home(team: dict[str, Any], gateway: Any, home: Path, runtime_url: str) -> None:
    import httpx

    now = datetime.now(timezone.utc)
    default_agent = team.get("default_agent") or team["agents"][0]["name"]
    (home / "config.yaml").write_text(f"default_agent: {default_agent}\n", encoding="utf-8")
    for spec in team["agents"]:
        name = spec["name"]
        workspace = home / "agents" / name / "workspace"
        gateway._runners[name].config.workspace = str(workspace)
        entry: dict[str, Any] = {
            "name": name,
            "model": spec.get("model", "olano:pro"),
            "description": spec.get("description", ""),
            "emoji": spec.get("emoji"),
            "backend": {"type": "filesystem", "root_dir": "workspace"},
            "org": {
                "title": spec.get("title"),
                "manager": spec.get("manager"),
                "reports": spec.get("reports", []),
            },
        }
        if spec.get("display_name"):
            entry["display_name"] = spec["display_name"]
        cfg_path = home / "agents" / name / "agent.json5"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text(json.dumps(entry, indent=2, ensure_ascii=False), encoding="utf-8")
        workspace.mkdir(parents=True, exist_ok=True)
        for rel, body in team.get("files", {}).get(name, {}).items():
            path = workspace / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(body, bytes):
                path.write_bytes(body)
            else:
                path.write_text(body, encoding="utf-8")

    # Earlier conversations: {agent: {thread_id: [(minutes_ago, role, content, extra?)]}}
    for agent, threads in team.get("threads", {}).items():
        sessions = home / "sessions" / agent
        sessions.mkdir(parents=True, exist_ok=True)
        for thread_id, rows in threads.items():
            with (sessions / f"{thread_id}.jsonl").open("w", encoding="utf-8") as fh:
                for row in rows:
                    minutes, role, content = row[0], row[1], row[2]
                    extra = row[3] if len(row) > 3 else {}
                    fh.write(
                        json.dumps(
                            {
                                "ts": (now - timedelta(minutes=minutes)).isoformat(),
                                "role": role,
                                "content": content,
                                **extra,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )

    with httpx.Client(base_url=runtime_url, timeout=30.0) as api:

        def call(method: str, url: str, **kw: Any) -> Any:
            resp = api.request(method, url, **kw)
            resp.raise_for_status()
            return resp.json()

        for secret in team.get("vault", []):
            call("POST", "/api/vault/secrets", json=secret)
        for agent, content in team.get("memory", {}).items():
            call("PUT", "/api/memory", params={"agent": agent}, json={"content": content})
        for agent, notes in team.get("notes", {}).items():
            for note in notes:
                call("POST", "/api/notes", params={"agent": agent}, json=note)
        for task in team.get("tasks", []):
            call("POST", "/api/scheduler/tasks", json={"timezone": "UTC", **task})
    # Issues go straight into the store, so agents can be their authors.
    from olano.issues.store import get_issue_store

    store = get_issue_store()
    for issue in team.get("issues", []):
        author = issue.get("author", "human-owner")
        created = store.create(
            title=issue["title"],
            body=issue.get("body", ""),
            author=author,
            assignee=issue.get("assignee", ""),
            labels=issue.get("labels", []),
            issue_type=issue.get("type", "task"),
        )
        for comment in issue.get("comments", []):
            actor, body = comment if isinstance(comment, list | tuple) else (author, comment)
            store.comment(created["id"], actor=actor, body=body)
        if issue.get("close"):
            closer = issue["close"] if isinstance(issue["close"], str) else author
            store.set_state(created["id"], actor=closer, state="closed")

    assert PUSH_EVENT is not None
    past = team.get("a2a", [])
    for i, msg in enumerate(past):
        sent = (now - timedelta(minutes=10 * (len(past) - i) + 10)).isoformat()
        replied = (now - timedelta(minutes=10 * (len(past) - i) + 9)).isoformat()
        base = {"kind": "peer", "from": msg["from"], "to": msg["to"], "message": msg["message"]}
        PUSH_EVENT("agent_message", {**base, "phase": "send", "ts": sent})
        PUSH_EVENT(
            "agent_message",
            {**base, "phase": "reply", "reply": msg["reply"], "ts_sent": sent, "ts_reply": replied},
        )


def adopt_new_agents(team: dict[str, Any], gateway: Any, home: Path, stop: threading.Event) -> None:
    """Give an agent created in the dashboard during a take a scripted runner.

    The launcher's gateway holds a fixed set of runners, so an agent made
    through the New agent dialog would exist on disk and never answer.
    """
    from olano.config.loader import load_agent_config

    while not stop.wait(0.3):
        agents_dir = home / "agents"
        if not agents_dir.is_dir():
            continue
        for path in agents_dir.iterdir():
            name = path.name
            if name in gateway._runners or not (path / "agent.json5").is_file():
                continue
            try:
                cfg = load_agent_config(name)
            except Exception:
                continue
            cfg.workspace = str(path / "workspace")
            (path / "workspace").mkdir(parents=True, exist_ok=True)
            gateway._runners[name] = ScriptedRunner(cfg, team.get("turns", {}).get(name, []))


def publish_external_agents(gateway: Any, home: Path, stop: threading.Event) -> None:
    """Keep the inbound MCP endpoint's tools in step with each agent's saved External access.

    The real runtime publishes an agent as ``ask_<agent>`` when it boots or restarts the agent
    (``runtime/server.py``: ``mcp_server.register_agent``). The launcher's gateway has no boot, so an
    agent switched on in the External access tab during a take would never be reachable. This reads
    each ``agent.json5`` and makes the same two calls, on the runtime's own loop; the endpoint, its
    token check and the tool call itself are the product's own code.
    """
    from olano.config.loader import load_agent_config

    def sync(name: str, runner: Any, access: Any) -> None:
        server = getattr(gateway, "mcp_server", None)
        if server is None or not hasattr(server, "register_agent"):
            return
        runner.config.external_access = access
        if access.mcp:
            server.register_agent(name, runner)
        else:
            server.unregister_agent(name)

    seen: dict[str, tuple[bool, str]] = {}
    while not stop.wait(0.3):
        for name, runner in list(gateway._runners.items()):
            if not (home / "agents" / name / "agent.json5").is_file():
                continue
            try:
                access = load_agent_config(name).external_access
            except Exception:
                continue
            state = (bool(access.mcp), str(access.description or ""))
            if seen.get(name, (False, "")) == state:
                continue
            seen[name] = state
            if LOOP is not None:
                LOOP.call_soon_threadsafe(sync, name, runner, access)


def main(argv: list[str]) -> int:
    global PUSH_EVENT, RUNTIME_URL, LOOP, GATEWAY
    demo = argv[0] if argv else "tour"
    team_mod = importlib.import_module(f"teams.{demo}")
    team: dict[str, Any] = team_mod.TEAM

    from harness import launch_dashboard

    logging.getLogger("httpx").setLevel(logging.WARNING)
    if team.get("vault"):
        # The vault opens itself from this key, as on a deployment that sets one.
        os.environ["OLANO_VAULT_KEY"] = "demo-passphrase-not-a-real-one"
        os.environ["LIONCLAW_VAULT_KEY"] = os.environ["OLANO_VAULT_KEY"]
    gateway = build_gateway(team)
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    with launch_dashboard(gateway) as dash:
        home = Path(os.environ["LIONCLAW_HOME"])
        PUSH_EVENT = dash.push_event
        RUNTIME_URL = dash.runtime_url
        LOOP = dash._loop_box[0]
        GATEWAY = gateway
        seed_home(team, gateway, home, dash.runtime_url)
        threading.Thread(
            target=adopt_new_agents, args=(team, gateway, home, stop), daemon=True
        ).start()
        threading.Thread(
            target=publish_external_agents, args=(gateway, home, stop), daemon=True
        ).start()
        after = getattr(team_mod, "after_seed", None)
        if after is not None:
            after(home=home, runtime_url=dash.runtime_url, gateway=gateway, dash=dash)
        print(
            json.dumps(
                {"base_url": dash.base_url, "runtime_url": dash.runtime_url, "home": str(home)}
            ),
            flush=True,
        )
        stop.wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
