"""ACP (Agent Client Protocol) server — exposes agents over ACP stdio/HTTP.

P6.7 adds multi-agent routing:
  GET  /acp/agents              — list all registered agents
  POST /acp/agents/{name}/run   — run task on specific agent (streaming SSE)
  POST /acp/agents/{name}/chat  — single-turn chat with agent
  POST /acp/delegate            — cross-agent delegation (fire-and-forget)
  GET  /acp/tasks/{id}          — check async task status
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from olano.core.checkpoints import scope_thread

logger = logging.getLogger(__name__)

# Global agent registry used by the multi-agent router.
# Maps agent_name → AgentRunner (or any callable with .ainvoke).
_REGISTERED_AGENTS: dict[str, Any] = {}
_TASK_STATUS: dict[str, dict[str, Any]] = {}


def register_agent_runner(name: str, runner: Any) -> None:
    """Register an agent runner so it is discoverable via /acp/agents."""
    _REGISTERED_AGENTS[name] = runner


def unregister_agent_runner(name: str) -> None:
    """Drop an agent from the ACP registry (called when the agent is stopped).

    Without this a stopped/restarted agent stays routable on a *sibling* agent's
    ACP server — the registry is process-global and one server routes every
    registered name — so `/acp/agents/<name>/chat` would keep driving a graph the
    runtime has torn down.
    """
    _REGISTERED_AGENTS.pop(name, None)


def list_registered_agents() -> list[str]:
    """Return names of all registered agents."""
    return list(_REGISTERED_AGENTS.keys())


def _acp_agent_model(agent_name: str) -> str | None:
    """Best-effort model string for ledger pricing — the registry stores bare
    compiled graphs, which don't carry their AgentConfig."""
    try:
        from olano.config.loader import load_agent_config

        return getattr(load_agent_config(agent_name), "model", None)
    except Exception:
        return None


async def _prior_message_ids(runner: Any, config: dict) -> set[str] | None:
    """Ids of the messages already on the thread, or None when unreadable.

    ACP threads persist (``acp_default`` is reused), so ``ainvoke`` returns the
    WHOLE history — recording it verbatim would re-ledger every prior turn on
    each call. The delta against these ids makes recording idempotent.
    """
    try:
        state = await runner.aget_state(config)
        msgs = (getattr(state, "values", None) or {}).get("messages", [])
        return {m.id for m in msgs if getattr(m, "id", None)}
    except Exception:
        return None


def _record_acp_usage(agent_name: str, result: Any, prior_ids: set[str] | None) -> None:
    """Ledger only the messages this turn added — ACP invokes run on the raw
    graph, so ``runner._record_usage`` never sees them. Skips (undercounts)
    when the prior state was unreadable or a message has no id; never
    double-counts."""
    if prior_ids is None or not isinstance(result, dict):
        return
    try:
        new_msgs = [
            m
            for m in result.get("messages", [])
            if getattr(m, "id", None) and m.id not in prior_ids
        ]
        if not new_msgs:
            return
        from olano.analytics.usage_recorder import record_result_usage

        record_result_usage(
            agent_name, _acp_agent_model(agent_name), {"messages": new_msgs}, origin="acp"
        )
    except Exception:  # pragma: no cover - accounting must not break the reply
        logger.debug("acp: usage recording failed", exc_info=True)


def _with_true_status(update: Any) -> Any:
    """A tool-call update whose result is a failure says so.

    deepagents-acp reports every live tool result as ``completed``, whatever
    came back. A call Olano refused (a path outside the workspace, a blocked
    command) or that raised reached the editor with a green tick over the
    refusal text. The result's own text is the evidence, read with the one
    definition the turn loop uses for a failed call.
    """
    if getattr(update, "session_update", None) != "tool_call_update":
        return update
    if getattr(update, "status", None) != "completed":
        return update
    from olano.core.tool_failures import is_failed_tool_result

    text = "".join(
        str(getattr(getattr(item, "content", None), "text", "") or "")
        for item in (getattr(update, "content", None) or [])
    )
    if not is_failed_tool_result(None, text):
        return update
    return update.model_copy(update={"status": "failed"})


class _TrueStatusClient:
    """The editor connection, with tool results reported by what they were."""

    def __init__(self, conn: Any) -> None:
        self._conn = conn

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)

    async def session_update(self, *args: Any, **kwargs: Any) -> Any:
        if "update" in kwargs:
            kwargs["update"] = _with_true_status(kwargs["update"])
        return await self._conn.session_update(*args, **kwargs)


class ACPServer:
    """Wraps a compiled deepagent graph in an ACP server.

    Usage:
        server = ACPServer(agent, config)
        await server.run_stdio()   # for editor integrations
        await server.run_http(port=8765)  # for HTTP-based ACP + multi-agent routes
    """

    def __init__(self, agent: Any, agent_name: str, system_prompt: str = "") -> None:
        self.agent = agent
        self.agent_name = agent_name
        self.system_prompt = system_prompt
        register_agent_runner(agent_name, agent)

    async def run_stdio(self) -> None:
        """Run ACP server in stdio mode (for editor integrations)."""
        try:
            from acp import run_agent
            from deepagents_acp.server import AgentServerACP

            # The ignore below is upstream's gap, not ours to close.
            # `AgentServerACP` explicitly subclasses `acp.interfaces.Agent`, which is
            # a `typing.Protocol`, and leaves `authenticate` / `close_session` /
            # `resume_session` unimplemented. That has always been true; it only
            # became visible to mypy in deepagents-acp 0.0.10, which fixed a typo in
            # the PEP 561 marker (0.0.9 shipped it as `py.typed.py`, so the package
            # read as untyped and every symbol resolved to `Any`). Runtime is
            # unaffected: a Protocol populates no `__abstractmethods__`, so the
            # instantiation below does not raise — verified against 0.0.11.
            # Drop this once upstream implements the three methods.
            server = AgentServerACP(self.agent)  # type: ignore[abstract]
            connect = server.on_connect
            server.on_connect = lambda conn: connect(_TrueStatusClient(conn))  # type: ignore[method-assign]
            logger.info("Starting ACP stdio server for agent '%s'", self.agent_name)
            await run_agent(server)
        except ImportError:
            raise ImportError("ACP support requires deepagents-acp: pip install deepagents-acp")

    def build_http_app(self) -> Any:
        """Build the aiohttp application with all ACP routes wired.

        Extracted from :meth:`run_http` so the routes (and their agent-scoped
        checkpoint keys) are unit-testable without binding a socket.
        """
        try:
            from aiohttp import web
        except ImportError:
            raise ImportError("ACP HTTP mode requires aiohttp: pip install aiohttp")

        # ── per-agent handlers ─────────────────────────────────────────────

        async def handle_run(request: web.Request) -> web.StreamResponse:
            agent_name = request.match_info.get("name", self.agent_name)
            runner = _REGISTERED_AGENTS.get(agent_name, self.agent)
            data = await request.json()
            message = data.get("message", "")
            # Scope the checkpoint thread by agent — the default "acp_default" is
            # otherwise identical for every agent, so two agents would share one
            # checkpoint lineage (the same cross-agent bleed the runner path fixes).
            thread_id = scope_thread(agent_name, data.get("thread_id", "acp_default"))

            response = web.StreamResponse()
            response.headers["Content-Type"] = "text/event-stream"
            await response.prepare(request)

            config = {"configurable": {"thread_id": thread_id}}
            prior_ids = await _prior_message_ids(runner, config)
            async for chunk in runner.astream(
                {"messages": [{"role": "user", "content": message}]},
                config=config,
                stream_mode="messages",
            ):
                if isinstance(chunk, tuple):
                    msg, _ = chunk
                    if hasattr(msg, "content") and isinstance(msg.content, str):
                        await response.write(f"data: {msg.content}\n\n".encode())
                elif hasattr(chunk, "content"):
                    await response.write(f"data: {chunk.content}\n\n".encode())

            await response.write(b"data: [DONE]\n\n")
            # Streamed run: no cost header exists mid-stream, so read the
            # settled state once and delta-record it (the recorder's token
            # estimate prices the managed tiers). Must never break the reply.
            try:
                state = await runner.aget_state(config)
                msgs = (getattr(state, "values", None) or {}).get("messages", [])
                _record_acp_usage(agent_name, {"messages": msgs}, prior_ids)
            except Exception:  # pragma: no cover - accounting never breaks the stream
                logger.debug("acp: post-stream usage recording failed", exc_info=True)
            return response

        async def handle_chat(request: web.Request) -> web.Response:
            agent_name = request.match_info.get("name", self.agent_name)
            runner = _REGISTERED_AGENTS.get(agent_name)
            if runner is None:
                return web.json_response({"error": f"Agent '{agent_name}' not found"}, status=404)
            data = await request.json()
            message = data.get("message", "")
            thread_id = scope_thread(agent_name, data.get("thread_id", "acp_default"))

            config = {"configurable": {"thread_id": thread_id}}
            prior_ids = await _prior_message_ids(runner, config)
            result = await runner.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config=config,
            )
            _record_acp_usage(agent_name, result, prior_ids)
            messages = result.get("messages", [])
            response_text = ""
            for msg in reversed(messages):
                if hasattr(msg, "type") and msg.type == "ai":
                    response_text = msg.content
                    break
            return web.json_response({"response": response_text, "agent": agent_name})

        # ── single-agent compat ────────────────────────────────────────────

        async def handle_invoke(request: web.Request) -> web.Response:
            data = await request.json()
            message = data.get("message", "")
            thread_id = scope_thread(self.agent_name, data.get("thread_id", "acp_default"))

            config = {"configurable": {"thread_id": thread_id}}
            prior_ids = await _prior_message_ids(self.agent, config)
            result = await self.agent.ainvoke(
                {"messages": [{"role": "user", "content": message}]},
                config=config,
            )
            _record_acp_usage(self.agent_name, result, prior_ids)
            messages = result.get("messages", [])
            response_text = ""
            for msg in reversed(messages):
                if hasattr(msg, "type") and msg.type == "ai":
                    response_text = msg.content
                    break
            return web.json_response({"response": response_text, "agent": self.agent_name})

        # ── multi-agent routing ────────────────────────────────────────────

        async def handle_agents_list(request: web.Request) -> web.Response:
            """GET /acp/agents — list all registered agents."""
            agents = [{"name": name, "status": "online"} for name in _REGISTERED_AGENTS]
            return web.json_response({"agents": agents, "count": len(agents)})

        async def handle_delegate(request: web.Request) -> web.Response:
            """POST /acp/delegate — async task delegation between agents."""
            import uuid

            data = await request.json()
            from_agent = data.get("from_agent", "unknown")
            to_agent = data.get("to_agent", "")
            task = data.get("task", "")
            # Scope by the callee (to_agent) — the delegated turn runs on that
            # agent's checkpoint, so its lineage must be bound to the callee.
            thread_id = scope_thread(to_agent, data.get("thread_id", "delegate_default"))

            if not to_agent or to_agent not in _REGISTERED_AGENTS:
                return web.json_response(
                    {"error": f"Agent '{to_agent}' not registered"}, status=404
                )

            task_id = str(uuid.uuid4())[:8]
            _TASK_STATUS[task_id] = {
                "id": task_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "task": task,
                "status": "queued",
                "result": None,
            }

            async def _run() -> None:
                _TASK_STATUS[task_id]["status"] = "running"
                try:
                    runner = _REGISTERED_AGENTS[to_agent]
                    config = {"configurable": {"thread_id": thread_id}}
                    prior_ids = await _prior_message_ids(runner, config)
                    result = await runner.ainvoke(
                        {"messages": [{"role": "user", "content": task}]},
                        config=config,
                    )
                    # Delegated turns run on the raw graph like chat/invoke —
                    # same delta recording, billed to the CALLEE (whose model
                    # and checkpoint the turn ran on).
                    _record_acp_usage(to_agent, result, prior_ids)
                    messages = result.get("messages", [])
                    response_text = ""
                    for msg in reversed(messages):
                        if hasattr(msg, "type") and msg.type == "ai":
                            response_text = msg.content
                            break
                    _TASK_STATUS[task_id]["status"] = "completed"
                    _TASK_STATUS[task_id]["result"] = response_text
                except Exception as exc:
                    _TASK_STATUS[task_id]["status"] = "error"
                    _TASK_STATUS[task_id]["result"] = str(exc)

            asyncio.create_task(_run())
            return web.json_response({"task_id": task_id, "status": "queued"})

        async def handle_task_status(request: web.Request) -> web.Response:
            """GET /acp/tasks/{id} — check async task status."""
            task_id = request.match_info["id"]
            status = _TASK_STATUS.get(task_id)
            if status is None:
                return web.json_response({"error": "task not found"}, status=404)
            return web.json_response(status)

        # ── wire routes ────────────────────────────────────────────────────

        app = web.Application()

        async def handle_health(request: web.Request) -> web.Response:
            return web.json_response({"agent": self.agent_name, "status": "ok"})

        # Legacy single-agent routes
        app.router.add_post("/run", handle_run)
        app.router.add_post("/invoke", handle_invoke)
        app.router.add_get("/health", handle_health)

        # Multi-agent routing (P6.7)
        app.router.add_get("/acp/agents", handle_agents_list)
        app.router.add_post("/acp/agents/{name}/run", handle_run)
        app.router.add_post("/acp/agents/{name}/chat", handle_chat)
        app.router.add_post("/acp/delegate", handle_delegate)
        app.router.add_get("/acp/tasks/{id}", handle_task_status)
        return app

    async def run_http(self, host: str = "127.0.0.1", port: int = 8765) -> None:
        """Run ACP over HTTP/SSE with multi-agent routing endpoints.

        Serves until cancelled, then **releases the socket**. The runtime runs
        this as a background task and cancels it when the agent stops; without
        the `finally` the AppRunner was never cleaned up, so the port stayed
        bound by the orphaned site and the agent's next boot could not re-bind
        it — an ACP toggle that worked once and never again after a restart.
        """
        from aiohttp import web

        app = self.build_http_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host, port)
        try:
            await site.start()
        except OSError:
            await runner.cleanup()
            raise
        logger.info(
            "ACP HTTP server for agent '%s' at http://%s:%d (multi-agent: /acp/agents)",
            self.agent_name,
            host,
            port,
        )

        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()
