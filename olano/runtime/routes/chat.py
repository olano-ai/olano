"""Chat turns, streaming, steering, the SSE event stream and voice transcription.

Split out of ``olano.runtime.api.create_api``; see ``olano.runtime.routes``.
"""

import asyncio
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request

from olano.core.runs import REGISTRY as _RUNS
from olano.core.runs import AgentRun
from olano.core.runs import sse_run_view as _sse_run_view
from olano.core.thread_labels import CHANNEL_MOBILE, CLIENT_METADATA_KEY
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer
from olano.runtime.agent_lifecycle import agent_unavailable_http_exc
from olano.security.request_context import AGENT_ADMIN_KEY

#: How often a turn paused on an approval re-checks whether anyone is still
#: watching it. The first check is immediate; this is how long a phone that goes
#: to the background *after* the approval card appeared waits for its push.
_APPROVAL_PUSH_POLL_S = 3.0


def _push_reply_if_unwatched(
    run: AgentRun, *, user_id: str, agent: str, thread_id: str, text: str, failed: bool
) -> None:
    """Tell the person's phone their reply is ready, if nobody is looking at it.

    "Looking" is a viewer attached to the run's stream: an open dashboard tab,
    or the app in the foreground. The app drops its streams when it goes to the
    background, so this is exactly the case where the answer would otherwise
    sit unseen. A no-op unless the runtime started the push dispatcher.
    """
    if not user_id or run.viewer_count > 0:
        return
    from olano.push import events as push_events

    push_events.reply_ready(
        user_id=user_id, agent=agent, thread_id=thread_id, text=text, failed=failed
    )


def register_chat_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the chat routes to ``app``."""
    # ── Chat ───────────────────────────────────────────────────────────────────

    @app.post("/api/chat")
    async def chat(body: _api.ChatRequest) -> dict:
        user = auth_layer.assert_agent_access(body.agent)
        runner = runtime._runners.get(body.agent)
        if not runner:
            raise agent_unavailable_http_exc(runtime, body.agent)

        thread_id = auth_layer.namespace_thread_id(body.thread_id)
        _api._assert_message_rate_limit(body.agent, runner)
        body.message = await _api._screen_message_policy(body.agent, runner, body.message or "")

        # Same attachment handling as /api/chat/stream, within ainvoke's
        # string-payload contract: media files are saved to the workspace and
        # referenced by path notes; text files are inlined. This endpoint
        # historically dropped attachments outright, so any failure just
        # reverts to the bare message. Member (non-admin) attachments are not
        # persisted (their environment is read-only), so their documents are
        # read in memory and inlined as text instead.
        message = body.message
        if body.attachments:
            from olano.runtime.chat_attachments import (
                ingest_chat_media,
                inline_chat_documents,
                transcribe_chat_audio,
            )

            try:
                if bool(getattr(user, "is_admin", False)):
                    text, remaining = await ingest_chat_media(
                        body.message, body.attachments, _api._workspace_root(body.agent), body.agent
                    )
                else:
                    text, remaining = body.message, body.attachments
                text, remaining = await inline_chat_documents(text, remaining)
                text, remaining = await transcribe_chat_audio(text, remaining)
                composed = _api._build_multimodal_content(text, remaining)
                # A non-string means leftover inline image blocks (save
                # failures); ainvoke takes strings, so keep the noted text.
                message = composed if isinstance(composed, str) else text
            except Exception:
                _api.logger.exception("chat attachment ingestion failed — sending bare message")
                message = body.message

        # Unlike /api/chat/stream this body runs inside the middleware's
        # context, so the human's identity is intact — but the middleware never
        # sees an agent name, so the per-agent ownership answer still has to be
        # added here (see AGENT_ADMIN_KEY).
        try:
            with _api.push_request_context(
                metadata={AGENT_ADMIN_KEY: auth_layer.can_admin_agent(user, body.agent)}
            ):
                response = await runner.ainvoke(message, thread_id=thread_id)
            _api._push_event(
                "chat_response",
                {"agent": body.agent, "thread_id": thread_id, "response": str(response)[:500]},
            )
            return {"response": response, "thread_id": thread_id, "agent": body.agent}
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ── Chat (streaming) ───────────────────────────────────────────────────────

    @app.post("/api/chat/stream")
    async def chat_stream(body: _api.ChatRequest, request: Request) -> Any:
        """SSE response that surfaces typed events from `astream_events`.

        Frame schema (one JSON object per `data:` line):
            {"type": "start",          "thread_id": str, "agent": str, "model": str, "turn_id": str}
            {"type": "media",          "role": "assistant", "items": [MediaRef]}  # once, before done
            {"type": "text",           "text": str}                        # streamed assistant text
            {"type": "steer",          "id": str, "text": str, "sender": str}  # a mid-turn message the model just read (POST /api/chat/steer)
            {"type": "subagent_text",  "text": str, "namespace": [...]}    # subagent output
            {"type": "tool_call",      "id": str, "name": str, "args": str}
            {"type": "tool_result",    "id": str, "name": str, "content": str}
            {"type": "status",         "kind": str, "message": str}
            {"type": "usage",          "input_tokens": int, "output_tokens": int, "context_tokens": int, "usd": float,
                                       "credits": float, "model_calls": int, ...}   # credits: Olano-managed turns only (gateway charge)
            {"type": "thread_switch",  "old_thread_id": str, "new_thread_id": str, "reason": str}
            {"type": "error",          "message": str}
            {"type": "done",           "thread_id": str, "turn_id": str, "trace_id": str?}  # trace_id: Langfuse, when traced
        """
        from starlette.responses import StreamingResponse as SR

        user = auth_layer.assert_agent_access(body.agent)
        runner = runtime._runners.get(body.agent)
        if not runner:
            raise agent_unavailable_http_exc(runtime, body.agent)
        # Whose phone hears about this turn when nobody is watching it (a reply
        # that landed, an approval it is waiting on). Captured here for the same
        # reason as everything below: the turn outlives the request context.
        push_user_id = str(getattr(user, "id", "") or "")

        # Media attachments (image/audio/video) are persisted to the agent's
        # workspace uploads/ and referenced by path — but only for admins:
        # non-admin members run in a read-only environment (no workspace
        # writes on their behalf), so their attachments keep the legacy
        # inline-vision path. Captured here because the SSE generator below
        # runs outside the request's auth context.
        persist_attachments = bool(getattr(user, "is_admin", False))
        # Same reason, for the self_config ownership gate: whether this human
        # may administer THIS agent needs the user record and the agent name
        # together, and neither survives into the generator. Resolve it here
        # (one indexed lookup, exactly like the `can_chat` check above) and
        # carry the answer, not the identity.
        agent_admin = auth_layer.can_admin_agent(user, body.agent)
        # And the human's display name, for the transcript's `sender` stamp
        # (`runner._append_session`). Every thread of an agent is shared by
        # everyone who may chat with it, so a user row has to say who wrote it
        # — platform turns and `/api/chat` already stamp it; the SSE body was
        # the one surface that lost the name. Single-user installs have one
        # human and keep the unstamped "You".
        human_name = ""
        if auth_layer.multi_user_enabled():
            human_name = str(
                getattr(user, "name", "")
                or getattr(user, "username", "")
                or getattr(user, "email", "")
                or ""
            ).strip()

        thread_id = auth_layer.namespace_thread_id(body.thread_id)
        _api._assert_message_rate_limit(body.agent, runner)
        body.message = await _api._screen_message_policy(body.agent, runner, body.message or "")
        # The client's handle on THIS turn, advertised in the `start` frame:
        # `POST /api/chat/steer` names it so a mid-turn message can never be
        # handed to a later turn on the same thread (core/steering.py).
        turn_id = uuid.uuid4().hex
        # Per-turn model override from the dashboard chat model picker. Empty /
        # equal-to-configured → no override (the agent runs its configured
        # model). An unbuildable model surfaces as a clean error frame from the
        # stream below (astream_events raises AgentError, caught in _generate).
        override_model = (body.model or "").strip() or None
        if override_model == getattr(runner.config, "model", ""):
            override_model = None
        override_effort = (body.reasoning_effort or "").strip().lower() or None
        if override_effort == getattr(runner.config, "reasoning_effort", None):
            override_effort = None
        model = override_model or getattr(runner.config, "model", "")
        mode = (body.mode or "auto").lower()

        # Manual/plan modes route every interrupt through the dashboard via
        # `/api/chat/approval`. Auto/act modes approve every interrupt inside
        # the runner and call the handler below only for what a person must
        # approve whatever the mode (a browser submit, purchase or send:
        # runner._always_asks_human). It is passed in every mode for that
        # reason: without it those approvals took the no-handler path, were
        # parked in the Approvals inbox instead of drawn in the chat the person
        # was watching, and the turn sat until `hitl_timeout_seconds` rejected
        # them.
        slot = _api._pending_approvals.setdefault(thread_id, _api._ApprovalSlot())
        hitl_timeout = (
            getattr(getattr(runner.config, "security", None), "hitl_timeout_seconds", 300) or 300
        )

        async def _await_decision(actions: list[dict]) -> Any:
            """Wait for the human's decision, pushing to their phone once if
            nobody is watching the turn, whether that is true when the approval
            appears or becomes true while it waits (the app went to the
            background with the card on screen). Raises ``TimeoutError`` after
            ``hitl_timeout`` like the plain wait it replaces.
            """
            loop = asyncio.get_running_loop()
            deadline = loop.time() + hitl_timeout
            pushed = False
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError
                if not pushed and push_user_id:
                    live = _RUNS.for_thread("chat", body.agent, thread_id)
                    if live is not None and live.viewer_count == 0:
                        pushed = True
                        from olano.push import events as push_events

                        push_events.chat_approval_needed(
                            user_id=push_user_id,
                            agent=body.agent,
                            thread_id=thread_id,
                            actions=actions,
                        )
                try:
                    # shield: a poll timing out must not cancel the slot's
                    # future, which /api/chat/approval is about to resolve.
                    return await asyncio.wait_for(
                        asyncio.shield(slot.future),
                        timeout=min(_APPROVAL_PUSH_POLL_S, remaining),
                    )
                except TimeoutError:
                    continue

        async def approval_handler(actions: list[dict]) -> list[dict]:
            # Wait until the user POSTs to /api/chat/approval; the slot's
            # future is replaced after each round so subsequent calls in the
            # same turn block again. A pending approval times out after the
            # agent's configured `hitl_timeout_seconds` so a turn never hangs
            # forever waiting on an absent human.
            try:
                decisions = await _await_decision(actions)
            except TimeoutError:
                slot.reset()
                _api.logger.warning(
                    "HITL approval timed out after %ss — rejecting [thread=%s]",
                    hitl_timeout,
                    thread_id,
                )
                return [{"type": "reject"} for _ in actions]
            except asyncio.CancelledError:
                # Stream cancelled mid-approval — treat as reject so the
                # runner cleans up gracefully.
                return [{"type": "reject"} for _ in actions]
            slot.reset()
            if not isinstance(decisions, list):
                return [{"type": "reject"} for _ in actions]
            # Pad / truncate to match the action count.
            out = list(decisions[: len(actions)])
            while len(out) < len(actions):
                out.append({"type": "reject"})
            return out

        # The turn itself. Everything below used to be the body of the SSE
        # response generator, and moving it here is the whole fix: a turn is now
        # a detached task that OUTLIVES the socket that started it, and the HTTP
        # response is a *view* of it (`_sse_run_view`). A phone that locks, an
        # app switch or a 5G→4G handover costs the viewer, never the work.
        #
        # `yield "data: …"` became `run.emit(frame)`, and the
        # `request.is_disconnected()` check is gone — nothing about this turn
        # depends on a client being there any more. The contextvar rebinding
        # below is unchanged and, notably, needed no adjustment: it was already
        # written for a caller running outside the request's context.
        async def _run_turn(run: AgentRun) -> None:
            run.emit(
                {
                    "type": "start",
                    "thread_id": thread_id,
                    "agent": body.agent,
                    "model": model,
                    "mode": mode,
                    "turn_id": turn_id,
                    "run_id": run.run_id,
                }
            )
            # Broadcast that this agent is now actively processing — the
            # playground avatar walks briskly to a desk, the dashboard tints
            # the Activity feed.
            _api._push_event("agent_activity", {"agent": body.agent, "status": "thinking"})
            # …and that a detached run exists on this thread, so a dashboard
            # that did not start it (another tab, another device, or this one
            # after a reload) can attach. Content-free by the same rule as
            # `groups_routes._publish`: an id and a kind, never message bodies.
            _api._push_event(
                "run_started",
                {
                    "agent": body.agent,
                    "thread_id": thread_id,
                    "run_id": run.run_id,
                    "turn_id": turn_id,
                    "kind": "chat",
                },
            )

            # Agent workspace root, resolved once — used to persist chat media
            # attachments below and to resolve the files the agent names in its
            # finished reply. (`_safe_media_refs` does the detection itself and
            # imports `media_refs` lazily; only the policy flag is needed here.)
            from olano.runtime.media_refs import SCAN_TOOL_RESULTS

            try:
                media_ws_root: Path | None = _api._workspace_root(body.agent)
            except Exception:
                media_ws_root = None

            # Save media attachments into the workspace and hand the agent
            # their paths (it decides whether to analyze the file with a
            # vision tool or use the path directly). Any failure falls back
            # to the legacy behavior — images inlined as vision blocks.
            message_text, wire_attachments = body.message, body.attachments
            if persist_attachments and media_ws_root is not None:
                from olano.runtime.chat_attachments import ingest_chat_media

                try:
                    message_text, wire_attachments = await ingest_chat_media(
                        message_text, wire_attachments, media_ws_root, body.agent
                    )
                except Exception:
                    _api.logger.exception(
                        "chat attachment ingestion failed — falling back to inline"
                    )
                    message_text, wire_attachments = body.message, body.attachments
            if wire_attachments:
                # Documents nothing persisted (a read-only member, a failed
                # ingest) are read in memory and inlined as text instead.
                from olano.runtime.chat_attachments import (
                    inline_chat_documents,
                    transcribe_chat_audio,
                )

                try:
                    message_text, wire_attachments = await inline_chat_documents(
                        message_text, wire_attachments
                    )
                except Exception:
                    _api.logger.exception("chat document inlining failed")
                # And recordings (a voice note from the mobile app) are
                # transcribed from a temp file: the inline path drops audio.
                try:
                    message_text, wire_attachments = await transcribe_chat_audio(
                        message_text, wire_attachments
                    )
                except Exception:
                    _api.logger.exception("chat audio transcription failed")
            payload = _api._build_multimodal_content(message_text, wire_attachments)

            # Bind the current agent name into a contextvar so any tool the
            # agent invokes (notably talk_to_agent) can attribute the call
            # back to the right caller. Reset in the finally block.
            from olano.tools.talk_to import current_agent as _ca

            _ca_token = _ca.set(body.agent)

            # Re-bind this turn's SURFACE. This body runs outside the request's
            # context — it always did (Starlette's BaseHTTPMiddleware had
            # already exited its own `with push_request_context(...)` by the
            # time the SSE generator ran), and now it also outlives the request
            # entirely — the same reason `persist_attachments`, `agent_admin`
            # and `human_name` are captured in the route above. Without this,
            # everything below, `runner._append_session` included, would observe
            # the unbound default context and record this turn as having come
            # from nowhere.
            #
            # Only `source`/`channel` are re-bound, never identity: putting
            # `user_id`/`is_owner`/`is_admin` back would change HITL and
            # tool-policy evaluation for every dashboard turn. That is a real
            # gap, and a security change that deserves its own review — not a
            # rider on a provenance fix.
            #
            # `agent_admin` is the one identity-derived fact that IS re-bound,
            # and deliberately so: it is additive (nothing reads it but the
            # self_config ownership gate), it was resolved above under the real
            # session, and without it that gate would see the unbound default
            # and pass for every dashboard human — i.e. not be a gate at all.
            # `name` is the second, for the same reason and with the same
            # shape: display-only, read by nothing but the transcript stamp.
            #
            # `session_id` IS the thread id on a turn — the platform path binds
            # exactly this (`runtime/server.py`: `session_id=str(thread_id or "")`),
            # and every consumer reads it that way: the middleware hook context,
            # the skill-awareness key, `talk_to`'s caller_thread, and the
            # magic-link/MCP-OAuth watchers, which freeze "which chat asked" from
            # it so they can report back long after the turn ended. Leaving it
            # empty is what made a dashboard-minted magic link unaddressable: it
            # was minted with `target: "owner"` and its completion notice had
            # nowhere to go. (Not a collision with the middleware's own
            # `session_id` — the `X-Olano-Session` header, for bearer-token
            # ingress: that context exited before this body runs, which is why
            # everything here has to be re-bound in the first place.)
            _turn_meta: dict[str, Any] = {AGENT_ADMIN_KEY: agent_admin}
            if human_name:
                _turn_meta["name"] = human_name
            if (body.client or "").strip().lower() == CHANNEL_MOBILE:
                _turn_meta[CLIENT_METADATA_KEY] = CHANNEL_MOBILE
            _turn_ctx = _api.push_request_context(
                source="api",
                channel="api",
                session_id=thread_id or "",
                metadata=_turn_meta,
            )
            _turn_ctx.__enter__()

            # The reply text, accumulated so the files the agent names in its
            # own words ("Saved: reports/x.png") can be surfaced once the turn
            # is complete. Since `SCAN_TOOL_RESULTS` went off this is the ONLY
            # thing that attaches a file to a dashboard turn.
            answer_parts: list[str] = []
            # Whether the agent's stream ran to completion — the difference
            # between "the turn broke" and "the answer landed and something
            # after it broke", which is what `record_turn_error` needs to tell
            # the agent whether there is anything to redo.
            stream_completed = False
            # How the turn ended, for the phone push in `finally`: a stopped
            # turn (Stop, or a runtime shutdown) and a reply the agent chose not
            # to give are never announced; a failure is, as a failure.
            turn_stopped = False
            turn_failed = False
            turn_declined = False
            try:
                async for ev in runner.astream_events(
                    payload,
                    thread_id=thread_id,
                    approval_handler=approval_handler,
                    session_mode=mode,
                    override_model=override_model,
                    override_reasoning_effort=override_effort,
                    turn_directive=_api._PLAN_MODE_PREAMBLE if mode == "plan" else None,
                    turn_id=turn_id,
                ):
                    if not isinstance(ev, tuple) or len(ev) != 2:
                        continue
                    kind, ev_payload = ev
                    frame: dict = {"type": kind}
                    if isinstance(ev_payload, dict):
                        frame.update(ev_payload)
                    elif isinstance(ev_payload, str):
                        frame["text"] = ev_payload
                    else:
                        frame["data"] = str(ev_payload)
                    # Namespaces (tuples) aren't JSON-serializable; flatten.
                    ns = frame.get("namespace")
                    if isinstance(ns, tuple):
                        frame["namespace"] = list(ns)
                    # Files a tool result merely NAMED are no longer attached —
                    # see `media_refs.SCAN_TOOL_RESULTS`. The client folds a
                    # tool result's media into the assistant message, so every
                    # path an intermediate tool printed (a read file's own
                    # contents included) became a card under the answer.
                    if kind == "tool_result" and media_ws_root is not None and SCAN_TOOL_RESULTS:
                        refs = _api._safe_media_refs(
                            str(frame.get("content", "")), media_ws_root, body.agent
                        )
                        if refs:
                            frame["media"] = refs
                    elif kind == "text" and isinstance(ev_payload, str):
                        answer_parts.append(ev_payload)
                    elif kind == "message_policy":
                        # The agent declined under `message_policy.ignore_rules`
                        # and the runner kept the marker off the stream, so this
                        # turn has no text at all. On a messenger that is the
                        # whole point; on the operator's own console it would
                        # read as a dropped connection, so reuse the `status`
                        # frame the client already draws (same shape as
                        # "Stopped.") and name the rule that fired.
                        frame = {
                            "type": "status",
                            "kind": "no_reply",
                            "message": _api._POLICY_IGNORED_DETAIL,
                        }
                        turn_declined = True
                    run.emit(frame)
                stream_completed = True
                # Files the agent named in its reply: one frame, after the text,
                # so the cards land under the finished answer (the transcript
                # reload derives the same list from the persisted row).
                if answer_parts and media_ws_root is not None:
                    reply_refs = _api._safe_media_refs(
                        "".join(answer_parts), media_ws_root, body.agent
                    )
                    if reply_refs:
                        run.emit({"type": "media", "role": "assistant", "items": reply_refs})
            except asyncio.CancelledError:
                # `POST /api/chat/stop`, or the runtime shutting down. Say so on
                # the run rather than letting it read as a silent truncation —
                # a stopped turn and a lost one look identical otherwise, which
                # is the confusion this whole module exists to end.
                turn_stopped = True
                run.emit(
                    {
                        "type": "status",
                        "kind": "stopped",
                        "message": "Stopped.",
                    }
                )
                raise
            except Exception as exc:
                from olano.core.actionable_errors import to_error_frame

                turn_failed = True
                error_frame = to_error_frame(exc)
                run.emit({"type": "error", **error_frame})
                # The user is now looking at something the agent has no record
                # of — the frame is drawn client-side and the assistant's row
                # was already written. Put it in the thread's context so the
                # obvious next question ("what is this error?") is answered from
                # the fact rather than from the wording.
                # Nothing in an error handler may raise a second error: the run
                # is already reporting a failure, and a throw here would end the
                # task on a different one.
                try:
                    runner.record_turn_error(
                        thread_id,
                        str(error_frame.get("message") or ""),
                        code=str(error_frame.get("code") or ""),
                        stage=(
                            "after_reply"
                            if stream_completed
                            else ("partial" if answer_parts else "turn")
                        ),
                    )
                except Exception:
                    _api.logger.debug("could not record turn error for the agent", exc_info=True)
            finally:
                try:
                    _turn_ctx.__exit__(None, None, None)
                except Exception:
                    pass
                # Wake any approval handler still awaiting a decision so the
                # runner doesn't leak. Then drop the slot.
                pending_slot = _api._pending_approvals.pop(thread_id, None)
                if pending_slot and not pending_slot.future.done():
                    pending_slot.future.set_result([])
                # Restore the contextvar so the next request doesn't inherit
                # this turn's caller name.
                try:
                    _ca.reset(_ca_token)
                except Exception:
                    pass
                # Broadcast that the agent went idle so the playground stops
                # darting it.
                _api._push_event("agent_activity", {"agent": body.agent, "status": "idle"})
                # Before `done`: the viewers are still attached here, so this
                # reads who was actually watching the turn as it ended.
                if not turn_stopped and not turn_declined:
                    try:
                        _push_reply_if_unwatched(
                            run,
                            user_id=push_user_id,
                            agent=body.agent,
                            thread_id=thread_id,
                            text="".join(answer_parts),
                            failed=turn_failed,
                        )
                    except Exception:
                        _api.logger.debug("reply push failed", exc_info=True)
                # `turn_id` names the reply for a feedback vote (core/feedback.py);
                # it is the id the runner stamped on the turn's assistant rows.
                done_frame: dict[str, Any] = {
                    "type": "done",
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                }
                # The turn's Langfuse trace, when it was traced: the admin
                # "Open in Langfuse" link is live from the moment the reply
                # lands, not only after a reload.
                try:
                    trace_id = runner.last_trace_id(thread_id)
                    if isinstance(trace_id, str) and trace_id:
                        done_frame["trace_id"] = trace_id
                except Exception:
                    pass
                run.emit(done_frame)
                _RUNS.finish(run)
                _api._push_event(
                    "run_ended",
                    {
                        "agent": body.agent,
                        "thread_id": thread_id,
                        "run_id": run.run_id,
                        "kind": "chat",
                    },
                )

        run = _RUNS.begin("chat", body.agent, thread_id, turn_id)
        if run is None:
            # A turn is already running on this thread. Two concurrent runs
            # would braid two replies into one transcript, so the client is
            # told which turn is live: it steers into it (`POST /api/chat/steer`)
            # or attaches to watch it, exactly as it already does when a turn is
            # in flight in its own tab.
            live = _RUNS.for_thread("chat", body.agent, thread_id)
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_in_progress",
                    "message": "This conversation is already working on a turn.",
                    "run_id": getattr(live, "run_id", ""),
                    "turn_id": getattr(live, "turn_id", ""),
                },
            )

        async def _drive() -> None:
            """Guarantee the run is closed out, whatever the turn does.

            ``_run_turn``'s own ``try`` opens only after attachment ingestion
            and the context bind, so a failure in either would escape it — and a
            run that never reaches :meth:`RunRegistry.finish` holds its thread
            key, 409-ing every later turn on that conversation. (The registry's
            dead-task sweep is the belt to this suspender; both are cheap.)
            """
            try:
                await _run_turn(run)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                from olano.core.actionable_errors import to_error_frame

                _api.logger.exception("chat run failed for %s", body.agent)
                run.emit({"type": "error", **to_error_frame(exc)})
                run.emit({"type": "done", "thread_id": thread_id})
                # `_run_turn`'s own finally never ran, so make its announcements
                # here: the playground must not stay "thinking", and the other
                # tabs must learn the run ended.
                _api._push_event("agent_activity", {"agent": body.agent, "status": "idle"})
                _api._push_event(
                    "run_ended",
                    {
                        "agent": body.agent,
                        "thread_id": thread_id,
                        "run_id": run.run_id,
                        "kind": "chat",
                    },
                )
            finally:
                _RUNS.finish(run)

        run.task = asyncio.get_running_loop().create_task(
            _drive(), name=f"chat-run:{body.agent}:{run.run_id}"
        )

        return SR(
            _sse_run_view(run, 0, request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/chat/approval")
    async def chat_approval(body: dict) -> dict:
        """Resolve the pending approval for a streaming chat thread.

        Body shape: ``{"thread_id": str, "decisions": [{"type": "approve"|"reject", ...}, ...]}``.
        Each decision in ``decisions`` corresponds 1:1 to the actions emitted
        in the matching ``approval`` SSE event.
        """
        thread_id = (body or {}).get("thread_id")
        decisions = (body or {}).get("decisions")
        if not thread_id or not isinstance(decisions, list):
            raise HTTPException(status_code=400, detail="thread_id and decisions[] are required")
        slot = _api._pending_approvals.get(thread_id)
        if not slot:
            raise HTTPException(status_code=404, detail="No streaming chat for this thread")
        if slot.future.done():
            raise HTTPException(
                status_code=409, detail="No outstanding approval; nothing to resolve"
            )
        slot.future.set_result(decisions)
        return {"ok": True}

    @app.get("/api/chat/attach")
    async def chat_attach(
        request: Request,
        agent: str,
        thread_id: str,
        run_id: str = "",
        cursor: int = 0,
    ) -> Any:
        """Rejoin a turn already running on this box.

        The recovery half of the detached run (:mod:`olano.core.runs`): a phone
        that came back from a lock screen, a tab reopened on another device, or
        a page reloaded mid-turn passes the last ``seq`` it saw and receives
        exactly what it missed, then tails live. Same frame schema as
        ``POST /api/chat/stream`` — deliberately, so the client folds an attach
        through the same reducer as the turn it started itself.

        ``404`` when no such run is live (it finished long enough ago to be
        swept, or the runtime restarted): the client falls back to refetching
        the transcript, which is what it already does today.

        ``run_id`` is optional — without it the run currently working on
        ``thread_id`` is served, which is what a client that only knows "this
        thread was busy" (a fresh page load) can ask for.
        """
        from starlette.responses import StreamingResponse as SR

        auth_layer.assert_agent_access(agent)
        # Same two gates as `GET /api/agents/{name}/threads/{id}`: the agent,
        # then the thread. A run is addressable only through a thread the caller
        # may see — a guessed `run_id` alone must never be enough.
        if not auth_layer.thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="no such run")
        scoped = auth_layer.namespace_thread_id(thread_id)
        run = _RUNS.get(run_id) if run_id else _RUNS.latest_for_thread("chat", agent, scoped)
        if run is None or run.agent != agent or run.thread_id != scoped:
            raise HTTPException(status_code=404, detail="no such run")
        return SR(
            _sse_run_view(run, max(0, int(cursor)), request),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @app.post("/api/chat/stop")
    async def chat_stop(body: dict) -> dict:
        """Stop the turn running on a thread.

        Now that a turn survives its socket, closing the connection no longer
        stops anything — so Stop needs a way to say so out loud. Cancelling the
        run task interrupts a model call already in flight, exactly as
        ``POST /api/groups/{id}/stop`` does for a cascade: a human who presses
        Stop must not have to wait out a turn that may take minutes.

        Whatever the agent already said stays in the transcript (the runner
        persists it on the way out), and any device watching the run sees the
        ``stopped`` status frame — so a turn someone else stopped never reads as
        a connection you lost.
        """
        agent = str((body or {}).get("agent") or "")
        if not agent:
            raise HTTPException(status_code=400, detail="agent is required")
        auth_layer.assert_agent_access(agent)
        raw_thread = str((body or {}).get("thread_id") or "")
        if not auth_layer.thread_visible_to_current_user(raw_thread):
            return {"ok": True, "stopped": False}
        run_id = str((body or {}).get("run_id") or "")
        scoped = auth_layer.namespace_thread_id(raw_thread)
        if run_id:
            run = _RUNS.get(run_id)
            if run is None or run.agent != agent or run.thread_id != scoped:
                return {"ok": True, "stopped": False}
            return {"ok": True, "stopped": run.stop()}
        return {"ok": True, "stopped": _RUNS.stop_thread("chat", agent, scoped)}

    @app.post("/api/chat/steer")
    async def chat_steer(body: _api.SteerRequest) -> dict:
        """Hand a message to the turn currently running on a thread (mid-turn steering).

        The runner appends it to the model's context at its next step — after
        the tool calls already in flight finish, before the next model call —
        without stopping anything; the model reads it as more user context and
        the stream reports it as a ``steer`` frame (plus a ``user`` transcript
        row stamped ``steer: true``). See ``core/steering.py``.

        ``409 {"code": "turn_ended"}`` when no turn is running on the thread or
        ``turn_id`` names one that already ended: the client sends the message
        as a normal turn instead. ``400`` for an empty/oversized message, one
        the input scan blocks, or a turn already holding the per-turn maximum.
        """
        from olano.core.runner import AgentError

        user = auth_layer.assert_agent_access(body.agent)
        runner = runtime._runners.get(body.agent)
        if not runner:
            raise agent_unavailable_http_exc(runtime, body.agent)
        text = (body.message or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="message is required")
        steer_id = (body.steer_id or "").strip()
        if steer_id and not _api._STEER_ID_RE.fullmatch(steer_id):
            raise HTTPException(
                status_code=400, detail="steer_id must be 8-64 characters of [A-Za-z0-9_-]"
            )
        thread_id = auth_layer.namespace_thread_id(body.thread_id)
        _api._assert_message_rate_limit(body.agent, runner)
        # A steer is text the model reads, so it is screened like any other
        # inbound message — otherwise "hi" followed by a 100k-character aside
        # walks straight around `max_input_chars`.
        text = await _api._screen_message_policy(body.agent, runner, text)
        # The transcript row names the human who typed the aside, not the one
        # who started the turn — same resolution as chat_stream's `sender`.
        sender = ""
        if auth_layer.multi_user_enabled():
            sender = str(
                getattr(user, "name", "")
                or getattr(user, "username", "")
                or getattr(user, "email", "")
                or ""
            ).strip()
        try:
            accepted = runner.steer(
                thread_id,
                text,
                turn_id=(body.turn_id or "").strip() or None,
                steer_id=steer_id or None,
                sender=sender,
            )
        except (AgentError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if accepted is None:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "turn_ended",
                    "message": "The agent is not working on this conversation right now "
                    "— send the message normally.",
                },
            )
        return {"accepted": True, "steer_id": accepted.id, "turn_id": accepted.turn_id}

    @app.post("/api/chat/steer/cancel")
    async def chat_steer_cancel(body: _api.SteerCancelRequest) -> dict:
        """Withdraw a steer the model has not read yet (the chip's Cancel / Edit).

        ``409 {"code": "already_delivered"}`` once the model has read it —
        nothing to withdraw, the transcript already shows it; ``404`` when no
        running turn holds that id (the turn ended, or it was never accepted).
        The client drops the chip on any answer.
        """
        auth_layer.assert_agent_access(body.agent)
        runner = runtime._runners.get(body.agent)
        if not runner:
            raise agent_unavailable_http_exc(runtime, body.agent)
        thread_id = auth_layer.namespace_thread_id(body.thread_id)
        outcome = runner.cancel_steer(thread_id, (body.steer_id or "").strip())
        if outcome == "cancelled":
            return {"cancelled": True}
        if outcome == "delivered":
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "already_delivered",
                    "message": "The agent has already read this message.",
                },
            )
        raise HTTPException(
            status_code=404,
            detail={"code": "unknown_steer", "message": "No pending message with that id."},
        )

    # ── SSE Stream ─────────────────────────────────────────────────────────────

    @app.get("/api/stream")
    async def event_stream(request: Request) -> Any:
        from starlette.responses import StreamingResponse as SR

        # Register a per-client queue. _push_event broadcasts to every entry
        # in _subscribers, so this client gets every event independently of
        # any other connected tab.
        my_queue: asyncio.Queue = asyncio.Queue(maxsize=500)
        _api._subscribers.append(my_queue)

        # Per-subscriber visibility, captured NOW: the generator below runs
        # outside the request's auth context. Members only receive frames
        # attributed to their granted agents (A2A frames need ≥1 granted
        # participant; raw log lines are admin-only). None → no filtering.
        allowed_agents = auth_layer.allowed_agent_names()
        # …and the thread namespace, for the same reason: a member granted an
        # agent still only sees their own conversations with it, live or not.
        thread_prefix = auth_layer.thread_visibility_prefix()

        async def _generate():
            loop = asyncio.get_event_loop()
            last_sent = loop.time()
            yield f"data: {json.dumps({'type': 'connected', 'ts': datetime.now(timezone.utc).isoformat()})}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(my_queue.get(), timeout=15.0)
                        if not auth_layer.event_visible_to_agents(
                            event, allowed_agents
                        ) or not auth_layer.event_visible_to_thread(event, thread_prefix):
                            # A member's filtered-out frames (e.g. every runtime
                            # `log` line) must not starve the byte stream: keep
                            # the connection warm so an idle-timeout proxy in
                            # front doesn't cut it. Emit a keepalive if enough
                            # time has passed since the last byte, then drop the
                            # frame.
                            if loop.time() - last_sent >= 15.0:
                                last_sent = loop.time()
                                yield ": keepalive\n\n"
                            continue
                        last_sent = loop.time()
                        yield f"data: {json.dumps(event)}\n\n"
                    except TimeoutError:
                        last_sent = loop.time()
                        yield ": keepalive\n\n"
                    except Exception:
                        break
            finally:
                try:
                    _api._subscribers.remove(my_queue)
                except ValueError:
                    pass

        return SR(
            _generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # ── Speech ───────────────────────────────────────────────────────────────

    # Providers validate an upload by its *filename extension*, so the name we
    # hand transcribe_file decides whether the call is accepted at all. Note
    # what is deliberately NOT reused here: olano/uploads.py maps audio/webm to
    # ".weba", which OpenAI-compatible endpoints reject — and webm is exactly
    # what MediaRecorder produces in Chrome and Firefox.
    _AUDIO_MIME_EXTENSIONS = {
        "audio/webm": ".webm",
        "video/webm": ".webm",
        "audio/mp4": ".m4a",  # Safari's MediaRecorder
        "video/mp4": ".m4a",
        "audio/x-m4a": ".m4a",
        "audio/mp4a-latm": ".m4a",
        "audio/ogg": ".ogg",
        "audio/opus": ".ogg",
        "audio/wav": ".wav",
        "audio/x-wav": ".wav",
        "audio/wave": ".wav",
        "audio/mpeg": ".mp3",
        "audio/mp3": ".mp3",
        "audio/flac": ".flac",
        "audio/x-flac": ".flac",
    }
    # Matroska/WebM EBML header. sniff_audio_format() covers ogg/wav/flac/m4a/
    # mp3/aac but has no Matroska branch, and webm is the common case here.
    _EBML_MAGIC = b"\x1a\x45\xdf\xa3"

    def _voice_upload_extension(data: bytes, mime: str, filename: str) -> str:
        """Pick the extension an STT provider will accept for this recording."""
        from olano.speech import audio_extension_for_format, sniff_audio_format

        sniffed = sniff_audio_format(data)
        if sniffed:
            return audio_extension_for_format(sniffed)
        if data[:4] == _EBML_MAGIC:
            return ".webm"
        ext = _AUDIO_MIME_EXTENSIONS.get((mime or "").split(";")[0].strip().lower())
        if ext:
            return ext
        suffix = Path(filename or "").suffix.lower()
        if suffix in {".webm", ".m4a", ".mp4", ".ogg", ".oga", ".wav", ".mp3", ".flac", ".mpga"}:
            return ".m4a" if suffix == ".mp4" else suffix
        # MediaRecorder's default in the browsers that have no Web Speech API.
        return ".webm"

    @app.post("/api/transcribe")
    async def transcribe_audio_upload(request: Request) -> dict:
        """Transcribe a dashboard voice recording with the configured STT provider.

        The fallback half of composer voice input: browsers with the Web Speech
        API dictate locally (free, live interim text) and never reach this
        route. Everyone else records with MediaRecorder and posts the blob here
        once, on release — which bills like any other STT call (olano credits on
        a managed box, the operator's own key under BYOK).

        Accepts multipart/form-data with a ``file`` field (plus an optional
        ``language`` hint) or a raw ``audio/*`` body. Errors carry a stable
        string code, not prose, because the dashboard localizes them.
        """
        import tempfile

        from olano.speech import SpeechDisabledError, SpeechNotConfiguredError, transcribe_file

        ct = (request.headers.get("content-type") or "").lower()
        language = ""
        filename = ""
        if ct.startswith("multipart/"):
            try:
                form = await request.form()
            except Exception as exc:
                raise HTTPException(status_code=400, detail="missing_file") from exc
            file = form.get("file")
            if file is None or not hasattr(file, "read"):
                raise HTTPException(status_code=400, detail="missing_file")
            data = await file.read()
            filename = getattr(file, "filename", "") or ""
            mime = getattr(file, "content_type", "") or ""
            language = str(form.get("language") or "").strip()
        else:
            # Raw-body path. Insist on an audio-ish content type: a form POST
            # that simply forgot the file field arrives here as
            # application/x-www-form-urlencoded, and treating its body as a
            # recording would ship "language=en" to the STT provider.
            base_ct = ct.split(";")[0].strip()
            if not (
                base_ct.startswith(("audio/", "video/")) or base_ct == "application/octet-stream"
            ):
                raise HTTPException(status_code=400, detail="missing_file")
            data = await request.body()
            mime = ct
            language = (request.query_params.get("language") or "").strip()

        if not data:
            raise HTTPException(status_code=400, detail="missing_file")
        # The OpenAI transcription API's own hard ceiling; anything larger is
        # rejected upstream anyway, so fail here rather than after the upload.
        if len(data) > 25 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="audio_too_large")

        ext = _voice_upload_extension(data, mime, filename)
        fd, tmp_path = tempfile.mkstemp(suffix=ext)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
            text = await asyncio.to_thread(transcribe_file, tmp_path, language)
        except SpeechDisabledError:
            raise HTTPException(status_code=409, detail="stt_disabled") from None
        except SpeechNotConfiguredError:
            raise HTTPException(status_code=409, detail="stt_not_configured") from None
        except HTTPException:
            raise
        except Exception as exc:
            _api.logger.warning("voice transcription failed: %s", exc)
            raise HTTPException(status_code=502, detail="stt_provider_error") from exc
        finally:
            try:
                Path(tmp_path).unlink()
            except OSError:
                pass

        return {"text": text or ""}

    _SPEAK_MIME = {
        ".mp3": "audio/mpeg",
        ".ogg": "audio/ogg",
        ".wav": "audio/wav",
        ".flac": "audio/flac",
        ".aac": "audio/aac",
        ".m4a": "audio/mp4",
    }

    @app.post("/api/speech/speak")
    async def speak_text(body: _api.SpeakRequest) -> Any:
        """Read words aloud in the box's voice (the mobile app's spoken replies).

        Synthesizes with the configured text-to-speech provider (Config ->
        Speech) under the agent's own credentials, and returns the audio itself:
        nothing is written into the workspace, so listening to a reply leaves no
        file behind. Asks for mp3 unless told otherwise, because every phone and
        browser plays it (Ogg/Opus, the voice-bubble format the messengers get,
        is not one an iPhone web view plays). Bills like any other speech call.

        Errors carry stable codes: ``tts_disabled`` / ``tts_not_configured``
        (409) tell the app to use the phone's own voice instead.
        """
        import tempfile

        from starlette.responses import Response

        from olano.speech import (
            TTS_AUDIO_FORMATS,
            SpeechDisabledError,
            SpeechNotConfiguredError,
            audio_extension_for_format,
            synthesize_speech,
        )
        from olano.tools._credentials import agent_runtime_context

        auth_layer.assert_agent_access(body.agent)
        runner = runtime._runners.get(body.agent)
        if not runner:
            raise agent_unavailable_http_exc(runtime, body.agent)
        text = (body.text or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="missing_text")
        requested = (body.audio_format or "").strip().lower()
        audio_format = requested if requested in TTS_AUDIO_FORMATS else "mp3"

        config = runner.config
        with tempfile.TemporaryDirectory(prefix="olano-speak-") as tmp:
            out = Path(tmp) / f"reply{audio_extension_for_format(audio_format)}"
            try:
                with agent_runtime_context(
                    config.name,
                    getattr(config, "env", None),
                    credential_scope=getattr(config, "credential_scope", None),
                ):
                    written = await asyncio.to_thread(
                        synthesize_speech, text, str(out), "", audio_format
                    )
                data = await asyncio.to_thread(Path(written).read_bytes)
            except SpeechDisabledError:
                raise HTTPException(status_code=409, detail="tts_disabled") from None
            except SpeechNotConfiguredError:
                raise HTTPException(status_code=409, detail="tts_not_configured") from None
            except Exception as exc:
                _api.logger.warning("speech synthesis failed: %s", exc)
                raise HTTPException(status_code=502, detail="tts_provider_error") from exc
        mime = _SPEAK_MIME.get(Path(written).suffix.lower(), "application/octet-stream")
        return Response(content=data, media_type=mime, headers={"Cache-Control": "no-store"})
