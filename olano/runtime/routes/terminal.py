"""Dashboard terminal WebSocket (``/api/terminal/ws``).

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import hmac
import os
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer


def register_terminal_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the terminal routes to ``app``."""
    from olano.runtime import terminal as terminal_mod

    def _terminal_origin_allowed(websocket: "WebSocket") -> bool:
        origin = websocket.headers.get("origin")
        if not origin:  # non-browser client (e.g. CLI) — no CSWSH risk
            return True
        from urllib.parse import urlsplit

        host = (urlsplit(origin).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        allowed = os.environ.get("LIONCLAW_DASHBOARD_ORIGIN", "").strip()
        if allowed:
            return host == (urlsplit(allowed).hostname or "").lower()
        # No explicit allowlist configured and a non-local origin: permit but
        # warn, so a custom-domain deploy works while the risk stays visible.
        _api.logger.warning(
            "terminal ws: allowing unconfigured origin %s (set LIONCLAW_DASHBOARD_ORIGIN)", origin
        )
        return True

    @app.websocket("/api/terminal/ws")
    async def terminal_ws(websocket: "WebSocket") -> None:
        if not terminal_mod.terminal_enabled():
            await websocket.close(code=4403)  # feature disabled
            return
        token = _api._auth_token()
        if token and not hmac.compare_digest(
            websocket.headers.get("authorization", "").encode("utf-8"),
            f"Bearer {token}".encode(),
        ):
            await websocket.close(code=1008)
            return
        if not _terminal_origin_allowed(websocket):
            await websocket.close(code=1008)
            return

        session_id = websocket.headers.get("x-olano-session", "").strip()
        ctx_kwargs = auth_layer.context_kwargs_for_request(websocket, session_id)
        with _api.push_request_context(**ctx_kwargs):
            if not auth_layer.can_use_terminal():
                await websocket.close(code=1008)  # auth / not granted terminal access
                return
            agent = websocket.query_params.get("agent") or None
            cwd = _api._workspace_root(agent)
            # Under a managed posture the host shell in the runtime container is
            # never exposed — the PTY opens INTO the agent's isolated sandbox.
            # Resolve the live container BEFORE accepting so we can refuse cleanly
            # when the sandbox isn't up for this agent (never a host-shell fallback).
            container: str | None = None
            if terminal_mod.terminal_uses_sandbox():
                container = terminal_mod.sandbox_container_ref(agent)
                if not container:
                    # Accept, THEN close. A close before the handshake is an
                    # HTTP 403 to the browser — the 4503 never reaches it, so
                    # the dashboard saw a generic 1006 and reconnected five
                    # times into the same missing container. Auth, origin and
                    # the terminal grant have all passed by this point, so
                    # accepting leaks nothing; the code is what lets the tab say
                    # "sandbox isn't running" instead of "session closed".
                    await websocket.accept()
                    await websocket.close(
                        code=4503, reason=terminal_mod.sandbox_unavailable_reason(agent)
                    )
                    return
            await websocket.accept()
            try:
                await terminal_mod.run_pty_bridge(websocket, cwd=cwd, container=container)
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                _api.logger.warning("terminal ws error: %s", exc)
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass
