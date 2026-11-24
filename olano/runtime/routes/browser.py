"""Browser control routes: status, the live image, per-agent sessions, the live view.

- ``GET  /api/browser/status``: effective mode (``browser_availability``),
  the live image's state, and the open sessions the caller may see.
- ``POST /api/browser/image/pull``: (admin) fetch the live browser image.
- ``GET  /api/agents/{name}/browser``: that agent's session, if any.
- ``POST /api/agents/{name}/browser/control``: ``{"who": "user"|"agent"}``:
  take control of the live browser, or hand it back.
- ``POST /api/agents/{name}/browser/recording``: ``{"action": "start"|"stop"}``:
  record the browser to an MP4 in the agent's workspace (``recordings/``),
  or stop and get the file (olano/browser/recording.py).
- ``POST /api/agents/{name}/browser/picture``: save the page as a PNG under
  the agent's ``screenshots/`` and get the file.
- ``POST /api/agents/{name}/browser/close``: close the session (sign-ins kept).
- ``POST /api/agents/{name}/browser/forget``: (agent admin) close it and
  delete the agent's saved sign-ins.
- ``WS   /api/agents/{name}/browser/live``: the live view: binary JPEG frames
  out, JSON input events and control changes in. A viewer may pace the
  frames itself: ``{"t": "view", "ack": true, "w": px, "h": px}`` says how
  big its picture is and that it will answer every frame with
  ``{"t": "shown"}`` once drawn; the next frame then waits for that answer
  (``ACK_TIMEOUT_S`` at most), frames are no larger than the picture, and a
  viewer slow to draw is stepped down to smaller, rougher frames until it
  keeps up (``live.QUALITY_LADDER``). A viewer that says nothing gets the
  stream as before.

Per-agent routes require the same grant as chatting with the agent, the live
view included (a screen can show an inbox, so it is as sensitive as the chat).
The WebSocket re-implements the HTTP gates, since the security middleware does
not run for WebSocket scopes (see the Terminal route), and adds the agent
grant the Terminal lacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
from collections import deque
from typing import Any

from fastapi import Body, FastAPI, HTTPException, WebSocket, WebSocketDisconnect

from olano.runtime import api as _api
from olano.runtime import auth as auth_layer

_main_loop: asyncio.AbstractEventLoop | None = None


def _capture_loop() -> None:
    global _main_loop
    try:
        _main_loop = asyncio.get_running_loop()
    except RuntimeError:
        pass


def _emit(event_type: str, data: Any) -> None:
    """``_push_event`` from any thread: the subscriber queues belong to uvicorn's loop.

    The loop is captured by the browser routes themselves (every handler calls
    :func:`_capture_loop`), and both dashboard surfaces that show browser state
    (Config → Browser, the agent's Browser tab) call one on load, so a viewer
    receives events from the moment it can show them.
    """
    loop = _main_loop
    if loop is None or loop.is_closed():
        return
    try:
        loop.call_soon_threadsafe(_api._push_event, event_type, data)
    except RuntimeError:
        pass


# A navigation waits up to 20 s for the page (olano/browser/live.py), so the
# budget per input has to outlast it.
INPUT_TIMEOUT_S = 30
INPUT_TIMEOUT_MESSAGE = (
    "The page did not take that input in time. It may be busy, or showing a dialog "
    "of the browser's own; try again, or press Reload."
)
# A tap or a key is replayed within this, or given up on: a page whose
# scripts hold its thread (a sign-in's verification step) answers no input,
# and a tap it finally takes seconds later lands on whatever is there by
# then. With it go the taps that queued behind it (:func:`drop_stale_input`),
# so one busy spell costs one message, not one per tap.
POINTER_TIMEOUT_S = 8
POINTER_TIMEOUT_MESSAGE = (
    "The page is not taking taps right now; it may be busy. Try again in a moment, or press Reload."
)
# A person acted and no frame followed: after this long the screencast is
# started over, which makes Chromium send one. A screencast can go quiet on a
# renderer swap or behind a window that came in front, and the person must
# never be left looking at a picture of the past.
POKE_AFTER_S = 2.5
# A viewer that paces the stream (``{"t": "view", "ack": true}``) gets the
# next frame once it says the last was drawn (``{"t": "shown"}``), or after
# this long, so a lost acknowledgement never stops the picture.
ACK_TIMEOUT_S = 1.5
# How long a viewer took to draw a frame, averaged, that steps it down
# QUALITY_LADDER to smaller frames, and back up; and the least time between
# two steps, so a hiccup does not swing the quality.
SLOW_ACK_S = 0.25
FAST_ACK_S = 0.08
LEVEL_HOLD_S = 3.0


# Inputs after which the state frame (address, tabs, history) should go out
# at once rather than on the next second's tick.
_NAVIGATION_KINDS = frozenset(
    {"navigate", "back", "forward", "reload", "stop", "tab", "newtab", "closetab"}
)
# Inputs on the shorter budget: each a DevTools round trip the page must take.
_POINTER_KINDS = frozenset(
    {"move", "down", "up", "wheel", "keydown", "keyup", "text", "select", "setvalue"}
)
# What is stale once a pointer input timed out. A release (``up``, ``keyup``)
# is kept: a button or key the page did take must be let go of.
_STALE_KINDS = frozenset({"move", "down", "wheel", "keydown", "text", "select", "setvalue"})
# Inputs a frame should follow (a move or a scroll may change nothing visible).
_ACTING_KINDS = frozenset({"down", "up", "keydown", "keyup", "text", "select", "setvalue", "wheel"})


# Handled the moment they arrive, never queued behind other input.
_OUT_OF_BAND = frozenset({"control", "dialog", "files", "stop", "view", "shown"})


def drop_stale_input(pending: deque[dict[str, Any]]) -> int:
    """Drop the taps, keys and scrolls waiting behind one that timed out;
    returns how many went. Navigation and the releases stay."""
    kept = [event for event in pending if event.get("t") not in _STALE_KINDS]
    dropped = len(pending) - len(kept)
    pending.clear()
    pending.extend(kept)
    return dropped


class FramePacing:
    """Which step of QUALITY_LADDER a viewer should be on, from how long it
    takes to draw each frame. Pure, so it is tested without a browser."""

    def __init__(self) -> None:
        self.level = 0
        self.average: float | None = None
        self.changed_at = 0.0

    def shown(self, latency_s: float, now: float, steps: int) -> int | None:
        """A frame took *latency_s* to come back acknowledged. Returns the new
        level when it should change, else ``None``."""
        latency = max(0.0, float(latency_s))
        self.average = latency if self.average is None else self.average * 0.7 + latency * 0.3
        if now - self.changed_at < LEVEL_HOLD_S:
            return None
        if self.average > SLOW_ACK_S and self.level < steps - 1:
            self.level += 1
        elif self.average < FAST_ACK_S and self.level > 0:
            self.level -= 1
        else:
            return None
        self.changed_at = now
        self.average = None  # judged afresh at the new size
        return self.level


def _enqueue_input(pending: deque[dict[str, Any]], event: dict[str, Any]) -> None:
    """Queue one live-view input event, folding it into the last one waiting
    when only the newest matters: a move replaces a move, wheel deltas add up."""
    kind = event.get("t")
    last = pending[-1] if pending else None
    if last is not None and kind == last.get("t"):
        if kind == "move":
            pending[-1] = event
            return
        if kind == "wheel":
            try:
                dx = float(last.get("dx", 0)) + float(event.get("dx", 0))
                dy = float(last.get("dy", 0)) + float(event.get("dy", 0))
            except (TypeError, ValueError):
                pass
            else:
                last["dx"], last["dy"] = dx, dy
                return
    pending.append(event)


def _availability() -> dict[str, Any]:
    from olano.browser.policy import browser_availability

    return browser_availability().to_dict()


async def _on_browser_loop(coro: Any, timeout_s: float = 30) -> Any:
    from olano.browser import loop as browser_loop

    return await browser_loop.run_async(coro, timeout_s=timeout_s)


async def _session_snapshot(agent: str) -> dict[str, Any] | None:
    from olano.browser import sessions

    async def _snap() -> dict[str, Any] | None:
        sess = sessions.get(agent)
        return sess.snapshot() if sess is not None else None

    snap: dict[str, Any] | None = await _on_browser_loop(_snap())
    return snap


def register_browser_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the browser routes to ``app``."""
    from olano.browser import sessions, sidecar

    sessions.on_change(lambda event: _emit("browser_session", event))
    sidecar.on_status_change(lambda status: _emit("browser_image", status))

    @app.get("/api/browser/status")
    async def browser_status() -> dict:
        _capture_loop()
        avail = await asyncio.to_thread(_availability)
        image = await asyncio.to_thread(sidecar.image_status)
        all_sessions = await _on_browser_loop(_list_sessions())
        visible = auth_layer.filter_by_agent_field(all_sessions, key="agent")
        return {"availability": avail, "image": image, "sessions": visible}

    async def _list_sessions() -> list[dict[str, Any]]:
        return sessions.all_sessions()

    @app.post("/api/browser/image/pull")
    async def browser_image_pull() -> dict:
        _capture_loop()
        auth_layer.assert_admin()
        return await asyncio.to_thread(sidecar.start_pull)

    @app.get("/api/agents/{name}/browser")
    async def agent_browser(name: str) -> dict:
        _capture_loop()
        auth_layer.assert_agent_access(name)
        avail = await asyncio.to_thread(_availability)
        session = await _session_snapshot(name)
        state_saved = await asyncio.to_thread(lambda: sessions.state_path(name).is_file())
        return {
            "agent": name,
            "availability": avail,
            "session": session,
            "live_view": avail.get("mode") == "live" and avail.get("provider") == "local",
            "signed_in_state": state_saved,
        }

    @app.post("/api/agents/{name}/browser/control")
    async def agent_browser_control(name: str, body: dict = Body(...)) -> dict:
        _capture_loop()
        auth_layer.assert_agent_access(name)
        who = str((body or {}).get("who") or "")
        if who not in ("user", "agent"):
            raise HTTPException(status_code=400, detail="who must be 'user' or 'agent'")
        avail = await asyncio.to_thread(_availability)
        if who == "user" and avail.get("mode") != "live":
            raise HTTPException(
                status_code=409, detail="Taking control needs the live browser (Config → Browser)."
            )
        try:
            sess = await _on_browser_loop(sessions.set_controller(name, who))
        except sessions.BrowserUnavailable as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return {"session": sess.snapshot()}

    def _saved_file(name: str, saved: Any) -> dict[str, Any]:
        from olano.runtime.media_refs import file_raw_url, files_view_url

        info: dict[str, Any] = saved.public()
        info["url"] = file_raw_url(saved.relative, name)
        info["files_url"] = files_view_url(saved.relative, name)
        return info

    @app.post("/api/agents/{name}/browser/recording")
    async def agent_browser_recording(name: str, body: dict = Body(...)) -> dict:
        _capture_loop()
        auth_layer.assert_agent_access(name)
        from olano.browser import recording

        action = str((body or {}).get("action") or "")
        if action not in ("start", "stop"):
            raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")
        try:
            if action == "start":
                root = await asyncio.to_thread(_api._workspace_root, name)
                state = await _on_browser_loop(recording.start_recording(name, root), timeout_s=60)
                return {"recording": state}
            saved = await _on_browser_loop(recording.stop_recording(name), timeout_s=90)
        except recording.RecordingUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if saved is None:
            return {"recording": None, "file": None}
        return {"recording": None, "file": _saved_file(name, saved)}

    @app.post("/api/agents/{name}/browser/picture")
    async def agent_browser_picture(name: str) -> dict:
        _capture_loop()
        auth_layer.assert_agent_access(name)
        from olano.browser import recording

        root = await asyncio.to_thread(_api._workspace_root, name)
        try:
            saved = await _on_browser_loop(recording.take_picture(name, root), timeout_s=60)
        except recording.RecordingUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"file": _saved_file(name, saved)}

    @app.post("/api/agents/{name}/browser/close")
    async def agent_browser_close(name: str) -> dict:
        _capture_loop()
        auth_layer.assert_agent_access(name)
        closed = await _on_browser_loop(sessions.close(name), timeout_s=90)
        return {"closed": bool(closed)}

    @app.post("/api/agents/{name}/browser/forget")
    async def agent_browser_forget(name: str) -> dict:
        _capture_loop()
        auth_layer.assert_agent_admin(name)
        await _on_browser_loop(sessions.close(name, save=False), timeout_s=90)
        removed = await asyncio.to_thread(sessions.forget_state, name)
        return {"forgotten": removed}

    @app.post("/api/agents/{name}/browser/human-check/skip")
    async def agent_browser_human_check_skip(name: str) -> dict:
        """The person chose not to pass the check the agent is waiting on: the
        agent stops waiting and moves on (olano.browser.human_check)."""
        _capture_loop()
        auth_layer.assert_agent_access(name)
        skipped = await _on_browser_loop(sessions.skip_human_check(name))
        if not skipped:
            raise HTTPException(status_code=409, detail="The agent is not waiting on a check.")
        return {"skipped": True}

    @app.get("/api/agents/{name}/browser/debug-trace")
    async def agent_browser_debug_trace(name: str) -> dict:
        """The running or last debug trace of this agent's browser
        (olano/browser/debug_trace.py): what the browser and the site said to
        each other around a check, and what that points to. Agent admins only:
        it lists the addresses the browser visited."""
        _capture_loop()
        auth_layer.assert_agent_admin(name)
        from olano.browser import debug_trace

        async def _read() -> dict | None:
            return debug_trace.latest(sessions.get(name), name)

        trace: dict | None = await _on_browser_loop(_read())
        if trace is None:
            raise HTTPException(status_code=404, detail="No debug trace recorded for this agent.")
        return trace

    @app.post("/api/agents/{name}/browser/debug-trace")
    async def agent_browser_debug_trace_control(name: str, body: dict = Body(...)) -> dict:
        """``{"action": "start"}`` records the open browser until ``stop``."""
        _capture_loop()
        auth_layer.assert_agent_admin(name)
        action = str((body or {}).get("action") or "")
        if action not in ("start", "stop"):
            raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")
        from olano.browser import debug_trace

        async def _act() -> dict | None:
            sess = sessions.get(name)
            if sess is None:
                return None
            if action == "start":
                return debug_trace.start(sess, "by hand").export()
            return debug_trace.stop(sess)

        result: dict | None = await _on_browser_loop(_act())
        if result is None:
            raise HTTPException(status_code=409, detail="This agent has no open browser.")
        return result

    # ── the plain browser test (olano/browser/plain_check.py) ─────────────────

    @app.get("/api/agents/{name}/browser/plain-check")
    async def agent_browser_plain_check(name: str) -> dict:
        """What the plain browser beside this agent's shows: each tab's title
        and address, and the exit address. Agent admins only."""
        _capture_loop()
        auth_layer.assert_agent_admin(name)
        from olano.browser import plain_check

        result = await asyncio.to_thread(plain_check.status, name)
        if result is None:
            raise HTTPException(status_code=404, detail="No plain browser test is running.")
        return result

    @app.post("/api/agents/{name}/browser/plain-check")
    async def agent_browser_plain_check_control(name: str, body: dict = Body(...)) -> dict:
        """``{"action": "start", "url": ..., "variant": "plain"|"attached"|
        "devtools-input"|"context", "parts": [...]}`` opens the page in a
        fresh plain browser (``parts``: plain_check.CONTEXT_PARTS; ``then_url``
        and ``then_after_s``: a second page in the same browser);
        ``{"action": "stop"}`` removes it."""
        _capture_loop()
        auth_layer.assert_agent_admin(name)
        from olano.browser import plain_check

        action = str((body or {}).get("action") or "")
        if action == "stop":
            return {"stopped": await plain_check.stop(name)}
        if action != "start":
            raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")
        avail = await asyncio.to_thread(_availability)
        if avail.get("mode") != "live" or avail.get("provider") != "local":
            raise HTTPException(
                status_code=409, detail="The plain browser test needs the live browser."
            )
        try:
            test = await plain_check.start(
                name,
                str((body or {}).get("url") or ""),
                str((body or {}).get("variant") or "plain"),
                [str(part) for part in ((body or {}).get("parts") or [])],
                then_url=str((body or {}).get("then_url") or ""),
                then_after_s=float((body or {}).get("then_after_s") or 20),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return test.public()

    @app.get("/api/agents/{name}/browser/plain-check/view")
    async def agent_browser_plain_check_view(name: str) -> Any:
        """A bare page to drive the test from: the display, and the controls."""
        auth_layer.assert_agent_admin(name)
        from fastapi.responses import HTMLResponse

        from olano.browser.plain_check_page import page_html

        return HTMLResponse(page_html(name), headers={"Cache-Control": "no-store"})

    @app.websocket("/api/agents/{name}/browser/plain-check/screen")
    async def agent_browser_plain_check_screen(websocket: WebSocket, name: str) -> None:
        _capture_loop()
        token = _api._auth_token()
        if token and not hmac.compare_digest(
            websocket.headers.get("authorization", "").encode("utf-8"),
            f"Bearer {token}".encode(),
        ):
            await websocket.close(code=1008)
            return
        if not _origin_allowed(websocket):
            await websocket.close(code=1008)
            return
        session_id = websocket.headers.get("x-olano-session", "").strip()
        ctx_kwargs = auth_layer.context_kwargs_for_request(websocket, session_id)
        with _api.push_request_context(**ctx_kwargs):
            try:
                auth_layer.assert_agent_admin(name)
            except HTTPException:
                await websocket.close(code=1008)
                return
            from olano.browser import plain_check

            await websocket.accept()
            test = plain_check.get(name)
            if test is None:
                await websocket.close(code=4404, reason="No plain browser test is running.")
                return
            try:
                await _run_plain_screen(websocket, plain_check.Screen(test))
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                _api.logger.warning("plain browser screen error: %s", exc)
                with contextlib.suppress(Exception):
                    await websocket.close(code=1011)

    def _human_check_payload(name: str) -> dict:
        from olano.config.loader import load_agent_config
        from olano.config.system_settings import load_browser_settings

        try:
            own = load_agent_config(name).browser
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {name}") from exc
        home = load_browser_settings()
        user = auth_layer.current_user()
        return {
            "agent": name,
            "human_check": own.human_check,
            "human_check_wait": own.human_check_wait,
            "home": {"human_check": home.human_check, "human_check_wait": home.human_check_wait},
            "can_edit": bool(user is not None and auth_layer.can_admin_agent(user, name)),
        }

    @app.get("/api/agents/{name}/browser/human-check")
    async def agent_browser_human_check(name: str) -> dict:
        auth_layer.assert_agent_access(name)
        return await asyncio.to_thread(_human_check_payload, name)

    @app.put("/api/agents/{name}/browser/human-check")
    async def agent_browser_human_check_save(name: str, body: dict = Body(...)) -> dict:
        # How long an agent may hold its browser waiting for a person is the
        # agent owner's decision, the same bar as its auto-approval rules.
        auth_layer.assert_agent_admin(name)

        def _save() -> None:
            from olano import admin
            from olano.config.agent_config import BrowserAgentConfig
            from olano.config.loader import load_agent_config

            browser = load_agent_config(name, source="home").browser.model_dump()
            for key in ("human_check", "human_check_wait"):
                if key in (body or {}):
                    browser[key] = (body or {}).get(key)
            BrowserAgentConfig.model_validate(browser)
            admin.update_agent(name, {"browser": browser})

        try:
            await asyncio.to_thread(_save)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(_human_check_payload, name)

    def _auto_approvals_payload(name: str) -> dict:
        from olano.browser.auto_approve import KIND_LABELS, current_rules

        try:
            rules = current_rules(name)
        except Exception as exc:
            raise HTTPException(status_code=404, detail=f"Unknown agent: {name}") from exc
        user = auth_layer.current_user()
        return {
            "agent": name,
            "rules": rules,
            "action_kinds": KIND_LABELS,
            "can_edit": bool(user is not None and auth_layer.can_admin_agent(user, name)),
        }

    @app.get("/api/agents/{name}/browser/auto-approvals")
    async def agent_browser_auto_approvals(name: str) -> dict:
        auth_layer.assert_agent_access(name)
        return await asyncio.to_thread(_auto_approvals_payload, name)

    @app.put("/api/agents/{name}/browser/auto-approvals")
    async def agent_browser_auto_approvals_save(name: str, body: dict = Body(...)) -> dict:
        # Loosening the browser's approval gate is an owner's decision: admins
        # and this agent's owners only, the same bar as forgetting its sign-ins.
        auth_layer.assert_agent_admin(name)
        rules = (body or {}).get("rules")
        if not isinstance(rules, list):
            raise HTTPException(status_code=400, detail="rules must be a list")
        from olano.browser.auto_approve import save_rules

        try:
            await asyncio.to_thread(save_rules, name, rules)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return await asyncio.to_thread(_auto_approvals_payload, name)

    def _origin_allowed(websocket: WebSocket) -> bool:
        check = getattr(_api, "_ws_origin_allowed", None)
        if check is not None:
            return bool(check(websocket))
        import os
        from urllib.parse import urlsplit

        origin = websocket.headers.get("origin")
        if not origin:
            return True
        host = (urlsplit(origin).hostname or "").lower()
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
            return True
        allowed = os.environ.get("LIONCLAW_DASHBOARD_ORIGIN", "").strip()
        if allowed:
            return host == (urlsplit(allowed).hostname or "").lower()
        return True

    @app.websocket("/api/agents/{name}/browser/live")
    async def agent_browser_live(websocket: WebSocket, name: str) -> None:
        _capture_loop()
        token = _api._auth_token()
        if token and not hmac.compare_digest(
            websocket.headers.get("authorization", "").encode("utf-8"),
            f"Bearer {token}".encode(),
        ):
            await websocket.close(code=1008)
            return
        if not _origin_allowed(websocket):
            await websocket.close(code=1008)
            return
        session_id = websocket.headers.get("x-olano-session", "").strip()
        ctx_kwargs = auth_layer.context_kwargs_for_request(websocket, session_id)
        with _api.push_request_context(**ctx_kwargs):
            try:
                auth_layer.assert_agent_access(name)
            except HTTPException:
                await websocket.close(code=1008)
                return
            avail = await asyncio.to_thread(_availability)
            await websocket.accept()
            if avail.get("mode") != "live" or avail.get("provider") != "local":
                await websocket.close(code=4403, reason="The live browser is not turned on.")
                return
            if await _session_snapshot(name) is None:
                await websocket.close(code=4404, reason="This agent has no open browser.")
                return
            try:
                await _run_live_view(websocket, name)
            except WebSocketDisconnect:
                pass
            except Exception as exc:  # pragma: no cover - defensive
                _api.logger.warning("browser live view error: %s", exc)
                try:
                    await websocket.close(code=1011)
                except Exception:
                    pass


async def _run_plain_screen(websocket: WebSocket, screen: Any) -> None:
    """The plain browser test's display: JPEG frames out while the screen
    changes, the person's input in, replayed in order."""
    from olano.browser.plain_check import FRAME_INTERVAL_S

    await screen.start()
    stop = asyncio.Event()

    async def send_frames() -> None:
        while not stop.is_set():
            frame = await screen.frame()
            if frame is not None:
                await websocket.send_bytes(frame)
            await asyncio.sleep(FRAME_INTERVAL_S)

    async def receive() -> None:
        while not stop.is_set():
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict):
                error = await screen.input(event)
                if error:
                    await websocket.send_text(json.dumps({"type": "error", "message": error}))

    tasks = [asyncio.create_task(send_frames()), asyncio.create_task(receive())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            task.result()
    finally:
        stop.set()
        for task in tasks:
            task.cancel()
        await screen.close()


async def _run_live_view(websocket: WebSocket, agent: str) -> None:
    from olano.browser import loop as browser_loop
    from olano.browser import sessions
    from olano.browser.live import (
        QUALITY_LADDER,
        Screencast,
        answer_dialog,
        answer_files,
        copy_selection,
        dispatch_input,
        live_state,
        picker_under,
        select_under,
    )

    main = asyncio.get_running_loop()
    frames: asyncio.Queue[bytes] = asyncio.Queue(maxsize=2)

    def _offer(data: bytes) -> None:
        # Keep only the newest frames: a slow viewer skips, it never lags.
        if frames.full():
            try:
                frames.get_nowait()
            except asyncio.QueueEmpty:
                pass
        frames.put_nowait(data)

    def on_frame(data: bytes, _meta: dict[str, Any]) -> None:
        main.call_soon_threadsafe(_offer, data)

    # Pointer events: where the agent is about to act (a screencast frame has
    # no cursor). Small and ordered, so a plain queue; only the newest matter
    # if a viewer falls behind.
    pointers: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=16)

    def _offer_pointer(event: dict[str, Any]) -> None:
        if pointers.full():
            try:
                pointers.get_nowait()
            except asyncio.QueueEmpty:
                pass
        pointers.put_nowait(event)

    def on_pointer(event: dict[str, Any]) -> None:
        main.call_soon_threadsafe(_offer_pointer, event)

    # Text a page's own copy button wrote while the person held control
    # (live.arm_page_copies), for their clipboard. Rare, so the newest few.
    copies: asyncio.Queue[str] = asyncio.Queue(maxsize=4)

    def _offer_copy(text: str) -> None:
        if copies.full():
            try:
                copies.get_nowait()
            except asyncio.QueueEmpty:
                pass
        copies.put_nowait(text)

    def on_copy(text: str) -> None:
        main.call_soon_threadsafe(_offer_copy, text)

    # What a page asked the browser for and could not have (a location, a
    # camera: live.arm_page_permissions), as a notice for the person.
    notices: asyncio.Queue[str] = asyncio.Queue(maxsize=4)

    def _offer_notice(text: str) -> None:
        if notices.full():
            try:
                notices.get_nowait()
            except asyncio.QueueEmpty:
                pass
        notices.put_nowait(text)

    def on_notice(text: str) -> None:
        main.call_soon_threadsafe(_offer_notice, text)

    # State frames go out when something changed (a page started or stopped
    # loading, navigated, a tab opened, a dialog appeared), and at least once
    # a second for what nothing announces (a page's title).
    state_wake = asyncio.Event()

    def wake_state(_what: str = "") -> None:
        main.call_soon_threadsafe(state_wake.set)

    async def _listen(add: bool) -> dict[str, Any] | None:
        sess = sessions.get(agent)
        if sess is None:
            return None
        if add:
            sess.pointer_listeners.add(on_pointer)
            sess.state_listeners.add(wake_state)
            sess.clipboard_listeners.add(on_copy)
            sess.notice_listeners.add(on_notice)
        else:
            sess.pointer_listeners.discard(on_pointer)
            sess.state_listeners.discard(wake_state)
            sess.clipboard_listeners.discard(on_copy)
            sess.notice_listeners.discard(on_notice)
        return sess.pointer

    cast = Screencast(agent, on_frame, on_change=wake_state)
    await browser_loop.run_async(cast.start(), timeout_s=30)
    last_pointer = await browser_loop.run_async(_listen(True), timeout_s=10)
    if last_pointer:
        await websocket.send_text(json.dumps({"type": "pointer", **last_pointer, "kind": "move"}))
    stop = asyncio.Event()

    # A viewer that paces the stream: the next frame goes out once it has
    # drawn the last (or ACK_TIMEOUT_S passed), and of the frames that came
    # meanwhile only the newest is sent. How long each took to come back
    # decides the frames' size and quality (FramePacing).
    viewer_paces = False
    drawn = asyncio.Event()
    drawn.set()
    sent_at = 0.0
    pacing = FramePacing()
    # The last input a frame should follow, for the watchdog.
    acted_at = 0.0
    poked_for = 0.0

    async def send_frames() -> None:
        nonlocal sent_at
        while not stop.is_set():
            data = await frames.get()
            if viewer_paces:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(drawn.wait(), ACK_TIMEOUT_S)
                drawn.clear()
                # Only the newest picture is wanted.
                while not frames.empty():
                    data = frames.get_nowait()
            sent_at = main.time()
            await websocket.send_bytes(data)

    async def send_pointers() -> None:
        while not stop.is_set():
            event = await pointers.get()
            await websocket.send_text(json.dumps({"type": "pointer", **event}))

    async def send_copies() -> None:
        while not stop.is_set():
            text = await copies.get()
            await websocket.send_text(json.dumps({"type": "clipboard", "text": text}))

    async def send_notices() -> None:
        while not stop.is_set():
            text = await notices.get()
            await websocket.send_text(json.dumps({"type": "notice", "message": text}))

    async def send_state() -> None:
        nonlocal poked_for
        last = None
        while not stop.is_set():
            await browser_loop.run_async(cast.follow(), timeout_s=10)
            snap = await _session_snapshot(agent)
            if snap is None:
                await websocket.send_text(json.dumps({"type": "closed"}))
                stop.set()
                break
            slim = {
                k: snap.get(k)
                for k in (
                    "controller",
                    "attention",
                    "human_check",
                    "url",
                    "kind",
                    "viewers",
                    "recording",
                )
            }
            # Cleared before reading, so a change while reading wakes the next
            # round instead of being wiped.
            state_wake.clear()
            try:
                extra = await browser_loop.run_async(live_state(agent, cast), timeout_s=10)
            except TimeoutError:
                extra = None  # a page too busy to answer: keep what was shown
            if extra:
                slim.update(extra)
            if slim != last:
                await websocket.send_text(json.dumps({"type": "state", "session": slim}))
                last = slim
            # The watchdog: a person acted, nothing came back, the picture
            # may be of the past. Once per input.
            if (
                acted_at > poked_for
                and acted_at > cast.last_frame_at
                and main.time() - acted_at > POKE_AFTER_S
            ):
                poked_for = acted_at
                with contextlib.suppress(Exception):
                    await browser_loop.run_async(cast.poke(), timeout_s=10)
            try:
                await asyncio.wait_for(state_wake.wait(), timeout=1.0)
            except TimeoutError:
                pass

    # Input is read as it arrives and replayed by a separate task. Each replay
    # is a DevTools round trip through the container's pipe, and a busy page
    # answers slowly; read and replayed in one loop, a pointer moving at 20
    # events a second can queue faster than it drains, and a tap then waits
    # behind seconds of stale moves (the view looks frozen until a reload drops
    # the socket and its backlog). Only the newest position matters, so a move
    # replaces a move still waiting and wheel deltas add up; everything else
    # keeps its order.
    pending: deque[dict[str, Any]] = deque()
    wake = asyncio.Event()

    async def receive() -> None:
        while not stop.is_set():
            raw = await websocket.receive_text()
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("t") in _OUT_OF_BAND:
                task = asyncio.create_task(handle_now(event))
                side_tasks.add(task)
                task.add_done_callback(side_tasks.discard)
                continue
            _enqueue_input(pending, event)
            wake.set()

    # What must not wait behind the input queue. The click or key that opened
    # a page's dialog does not return until the dialog is answered, and a
    # navigation holds the queue until it loads, so an answer, Stop or Hand
    # back queued behind them would only land once they gave up (30 s).
    side_tasks: set[asyncio.Task[None]] = set()

    async def say_error(message: str) -> None:
        with contextlib.suppress(Exception):
            await websocket.send_text(json.dumps({"type": "error", "message": message}))

    async def handle_now(event: dict[str, Any]) -> None:
        nonlocal viewer_paces
        kind = event.get("t")
        try:
            if kind == "view":
                # The viewer says how big its picture is (device pixels) and
                # whether it paces the stream.
                if event.get("ack"):
                    viewer_paces = True
                    drawn.set()
                if event.get("w") is not None and event.get("h") is not None:
                    await browser_loop.run_async(
                        cast.set_viewer(event.get("w"), event.get("h")), timeout_s=10
                    )
                return
            if kind == "shown":
                # The viewer drew the last frame: the next may go, and how long
                # this one took decides the size of the ones after it.
                if sent_at:
                    level = pacing.shown(main.time() - sent_at, main.time(), len(QUALITY_LADDER))
                    if level is not None:
                        with contextlib.suppress(Exception):
                            await browser_loop.run_async(cast.set_level(level), timeout_s=10)
                drawn.set()
                return
            if kind == "control":
                who = str(event.get("who") or "")
                if who in ("user", "agent"):
                    await browser_loop.run_async(sessions.set_controller(agent, who))
                return
            if kind == "dialog":
                error = await browser_loop.run_async(answer_dialog(agent, event), timeout_s=30)
            elif kind == "files":
                error = await browser_loop.run_async(answer_files(agent, event), timeout_s=60)
            else:  # stop
                error = await browser_loop.run_async(dispatch_input(agent, event), timeout_s=15)
            if error:
                await say_error(error)
        except TimeoutError:
            await say_error(INPUT_TIMEOUT_MESSAGE)
        except Exception as exc:
            await say_error(str(exc))
        finally:
            wake_state()

    async def offer_select(event: dict[str, Any]) -> None:
        # A click that opened a <select>, or landed on a date, time or colour
        # field: the list or the picker is drawn by the browser, out of the
        # picture, so the viewer shows its own. Off the input queue: every
        # click pays these reads, and the next tap must not.
        with contextlib.suppress(Exception):
            options = await browser_loop.run_async(
                select_under(agent, event, cast.size()), timeout_s=10
            )
            if options:
                await websocket.send_text(json.dumps({"type": "select", **options}))
                return
            picker = await browser_loop.run_async(
                picker_under(agent, event, cast.size()), timeout_s=10
            )
            if picker:
                await websocket.send_text(json.dumps({"type": "picker", **picker}))

    async def dispatch() -> None:
        nonlocal acted_at
        while not stop.is_set():
            if not pending:
                wake.clear()
                await wake.wait()
                continue
            event = pending.popleft()
            kind = event.get("t")
            if kind in ("copy", "cut"):
                text, error = await browser_loop.run_async(
                    copy_selection(agent, cut=event.get("t") == "cut"), timeout_s=15
                )
                if error:
                    await websocket.send_text(json.dumps({"type": "error", "message": error}))
                else:
                    await websocket.send_text(json.dumps({"type": "clipboard", "text": text}))
                continue
            pointer = kind in _POINTER_KINDS
            try:
                # The viewer's fractions span the frame it is looking at.
                error = await browser_loop.run_async(
                    dispatch_input(agent, event, cast.size()),
                    timeout_s=POINTER_TIMEOUT_S if pointer else INPUT_TIMEOUT_S,
                )
            except TimeoutError:
                if pointer:
                    drop_stale_input(pending)
                    error = POINTER_TIMEOUT_MESSAGE
                else:
                    error = INPUT_TIMEOUT_MESSAGE
            if error:
                await websocket.send_text(json.dumps({"type": "error", "message": error}))
            elif kind == "up" and event.get("button", "left") == "left":
                task = asyncio.create_task(offer_select(event))
                side_tasks.add(task)
                task.add_done_callback(side_tasks.discard)
            if kind in _ACTING_KINDS or kind in _NAVIGATION_KINDS:
                acted_at = main.time()
            if kind in _NAVIGATION_KINDS:
                wake_state()

    tasks = [
        asyncio.create_task(t())
        for t in (
            send_frames,
            send_pointers,
            send_copies,
            send_notices,
            send_state,
            receive,
            dispatch,
        )
    ]
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            exc = task.exception()
            if exc is not None and not isinstance(exc, WebSocketDisconnect):
                raise exc
    finally:
        stop.set()
        for task in [*tasks, *side_tasks]:
            task.cancel()
        try:
            await browser_loop.run_async(_listen(False), timeout_s=10)
            await browser_loop.run_async(cast.stop(), timeout_s=10)
        except Exception:
            pass
        try:
            await websocket.close()
        except Exception:
            pass
