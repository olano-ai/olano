"""Thumbs up / down on a reply, and the per-agent counts Insights shows.

The votes are stored on the box (``olano.core.feedback``); a vote on a traced
turn is also sent to Langfuse as a score, in the background
(``olano.observability.feedback``). Anyone who may read the thread may vote on
it: the same two checks ``GET …/threads/{id}`` makes.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from olano.core import feedback as store
from olano.runtime import api as _api
from olano.runtime import auth as auth_layer

# Background Langfuse score sends, held so the loop does not drop them.
_SCORE_SENDS: set[asyncio.Task[Any]] = set()


class FeedbackBody(BaseModel):
    agent: str
    thread_id: str
    turn_id: str
    value: int
    comment: str = Field(default="", max_length=store.COMMENT_MAX_CHARS * 2)


def register_feedback_routes(app: FastAPI, runtime: Any, closures: dict[str, Any]) -> None:
    """Attach the feedback routes to ``app``."""

    @app.post("/api/chat/feedback")
    async def chat_feedback(body: FeedbackBody) -> dict[str, Any]:
        """Record the caller's vote on one reply (``value`` 1, -1, or 0 to
        take it back) and return it. 404 when the thread or the turn is not
        there; a reply written before turns were stamped cannot be voted on."""
        if not (store.safe_thread_id(body.agent) and store.safe_thread_id(body.thread_id)):
            raise HTTPException(status_code=404, detail="Thread not found")
        user = auth_layer.assert_agent_access(body.agent)
        if not auth_layer.thread_visible_to_current_user(body.thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")
        if body.value not in store.VALUES:
            raise HTTPException(status_code=422, detail="value must be 1, -1 or 0")
        turn_id = body.turn_id.strip()
        trace_id = (
            await asyncio.to_thread(store.turn_trace_id, body.agent, body.thread_id, turn_id)
            if turn_id
            else None
        )
        if trace_id is None:
            raise HTTPException(status_code=404, detail="Reply not found")
        vote = await asyncio.to_thread(
            store.record_vote,
            body.agent,
            body.thread_id,
            turn_id,
            user=user.id,
            value=body.value,
            comment=body.comment,
            trace_id=trace_id,
        )
        runner = runtime._runners.get(body.agent)
        config = getattr(runner, "config", None)
        if trace_id and config is not None:
            from olano.observability.feedback import send_feedback

            task = asyncio.get_running_loop().create_task(
                asyncio.to_thread(
                    send_feedback,
                    config,
                    trace_id,
                    user=user.id,
                    value=vote.value,
                    comment=vote.comment,
                )
            )
            _SCORE_SENDS.add(task)
            task.add_done_callback(_SCORE_SENDS.discard)
        return {
            "ok": True,
            "turn_id": vote.turn_id,
            "value": vote.value,
            "comment": vote.comment,
        }

    @app.get("/api/analytics/feedback")
    async def analytics_feedback(days: int = 30, hours: Optional[int] = None) -> dict[str, Any]:
        """Standing thumbs up / down per agent over the Insights window
        (``days`` 1-366, or ``hours`` 1-168 like ``/api/analytics/overview``),
        scoped to the caller's agents. Read from the box, never from Langfuse."""
        if hours is not None:
            span = timedelta(hours=max(1, min(int(hours), 168)))
        else:
            span = timedelta(days=max(1, min(int(days or 30), 366)))
        allowed = auth_layer.allowed_agent_names()
        since = datetime.now(timezone.utc) - span
        try:
            return await asyncio.to_thread(
                store.summarize, since, set(allowed) if allowed is not None else None
            )
        except Exception:
            _api.logger.debug("feedback summary failed", exc_info=True)
            return {"since": since.isoformat(), "up": 0, "down": 0, "by_agent": []}
