"""Agent Father greets a brand-new owner the moment setup finishes.

The onboarding wizard ends in Agent Father's chat, and the first thing the
owner reads there should be a hello rather than an empty composer. So when
``POST /api/onboarding/complete`` lands, the runtime runs ONE turn on a fresh
thread with a prompt the platform wrote ("a new owner just signed up: …, say
hi") and the dashboard opens that thread. The prompt itself is hidden from the
transcript (``HIDDEN_PROMPT_KEY`` → the runner stamps the row ``hidden`` and
``api._transcript_message`` skips it); the reply is what shows.

Three properties are load-bearing:

* **Never in the request's critical path.** ``schedule_greeting`` returns the
  thread pointer immediately and the turn runs as a background task — a model
  call can take ten seconds, and the wizard must not sit on a spinner for it.
* **Never a second hello.** The thread id is deterministic per human
  (``welcome-<user id>``), and a thread that already has rows is left alone —
  re-running the tour (``DELETE /api/onboarding``) lands the owner on the
  greeting they already have.
* **Never a failure the owner can see.** A missing runner, a withheld plan
  feature, a model error: each is logged and costs a hello, nothing else. The
  chat still opens, and the first human message surfaces whatever is wrong
  through the normal actionable-error path.

And one thing it does on top of greeting: **it starts Agent Father** when it
is installed but not running (``_start_agent_father``). The tour lands in that
chat, and an offline agent there was what a new owner met on a managed box —
the cloud installs Agent Father after the runtime has taken its roster
snapshot, the wizard's install POST just answers 409, and nothing else on the
completion path booted it.

The thread id carries none of ``thread_labels.SYSTEM_THREAD_PREFIXES`` and no
``:`` on purpose: it must read as an ordinary dashboard conversation — not a
SCHEDULED badge, not excluded from the digest — because that is what it is
from the owner's side.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from olano.security.request_context import (
    HIDDEN_PROMPT_KEY,
    ORIGIN_KEY,
    make_origin,
    push_request_context,
)

logger = logging.getLogger(__name__)

WELCOME_AGENT = "agentfather"
THREAD_LABEL = "Welcome"
# How long the background turn waits for Agent Father's runner once a boot is
# in flight — the wizard's last step may have installed it seconds ago, or the
# greeting itself just started it (see ``_start_agent_father``). A cold boot
# pays for the model build, every MCP handshake and the RAG ingest, and can run
# past a minute on a small box; the turn is a background task, so waiting
# costs nobody anything but a later hello.
RUNNER_WAIT_SECONDS = 300.0
RUNNER_POLL_SECONDS = 0.5

# Keep strong references to in-flight tasks: asyncio holds only weak ones, and
# a greeting garbage-collected mid-turn is a hello that never arrives.
_TASKS: set[asyncio.Task[Any]] = set()


def welcome_thread_id(user_id: str) -> str:
    """Deterministic per human; no system prefix, no ``:`` (see module doc)."""
    return f"welcome-{user_id}"


def build_greeting_prompt(user: Any) -> str:
    """The instruction Agent Father acts on. Pure, so the wording is testable."""
    name = str(getattr(user, "name", "") or "").strip() or "the new owner"
    handle = str(getattr(user, "username", "") or "").strip()
    email = str(getattr(user, "email", "") or "").strip()
    who = name
    details = [d for d in (f"@{handle}" if handle else "", email) if d]
    if details:
        who = f"{name} ({', '.join(details)})"
    return (
        f"A new owner just signed up: {who}. This is the first time they open the "
        "dashboard, and this message comes from the platform, not from them. Say hi, "
        "introduce yourself, and tell them in a few lines what you can do for them — "
        "creating agents from a description, reconfiguring them, connecting messaging "
        "channels and services, inviting teammates. Keep it short and warm, and end by "
        "asking what they would like to build first. Do not change anything and do not "
        "call tools that modify the deployment in this turn — just greet them."
    )


def greeting_available(runtime: Any) -> bool:
    """Can this box greet — is Agent Father here and not withheld by the plan?"""
    blocked = getattr(runtime, "_blocked_agents", None)
    if isinstance(blocked, dict) and WELCOME_AGENT in blocked:
        return False
    runners = getattr(runtime, "_runners", None) or {}
    if WELCOME_AGENT in runners:
        return True
    try:
        from olano.config.loader import find_agent_config_path

        return find_agent_config_path(WELCOME_AGENT) is not None
    except Exception:  # pragma: no cover - defensive
        logger.debug("agentfather lookup failed", exc_info=True)
        return False


def schedule_greeting(runtime: Any, user: Any) -> dict[str, str] | None:
    """Kick off the greeting turn; return where the dashboard should land.

    ``None`` means "no greeting on this box" and the dashboard falls back to
    Overview. Called from the completion route on the event loop.
    """
    if not greeting_available(runtime):
        return None
    thread_id = welcome_thread_id(str(getattr(user, "id", "") or "owner"))
    try:
        task = asyncio.get_running_loop().create_task(
            greet_new_owner(runtime, user, thread_id), name=f"welcome-greeting:{thread_id}"
        )
    except RuntimeError:  # no running loop (sync test harness) → nothing to schedule
        logger.debug("welcome greeting not scheduled: no running loop")
        return {"agent": WELCOME_AGENT, "thread_id": thread_id}
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return {"agent": WELCOME_AGENT, "thread_id": thread_id}


async def _wait_for_runner(runtime: Any, wait_seconds: float, poll: float) -> Any | None:
    deadline = asyncio.get_running_loop().time() + max(0.0, wait_seconds)
    while True:
        runner = (getattr(runtime, "_runners", None) or {}).get(WELCOME_AGENT)
        if runner is not None:
            return runner
        if asyncio.get_running_loop().time() >= deadline:
            return None
        await asyncio.sleep(poll)


def _paused_by_owner() -> bool:
    try:
        from olano.config.agent_state import is_agent_paused

        return is_agent_paused(WELCOME_AGENT)
    except Exception:  # pragma: no cover - an unreadable flag reads as "not paused"
        logger.debug("paused-state lookup failed for %s", WELCOME_AGENT, exc_info=True)
        return False


async def _start_agent_father(runtime: Any) -> bool:
    """Boot Agent Father when it is on disk but not running. Returns True when
    this call booted it (or a boot was already under way).

    The tour ends in Agent Father's chat, so an offline Agent Father there is
    the first thing a new owner sees — and nothing else on the completion path
    started it: the wizard's install POST answers 409 on a managed box (the
    cloud installed it), and the runtime only boots what was on disk when it
    came up. So the greeting starts it, through the same ``_boot_agent`` the
    dashboard's Start button uses (one boot at a time per agent; the status
    broadcast flips the roster live).

    Deliberately not started: an agent the owner **paused** (that is a
    decision, and the chat's own Start button honours it the same way), and
    one a boot is already in flight for (waiting is enough).
    """
    runners = getattr(runtime, "_runners", None) or {}
    if WELCOME_AGENT in runners:
        return False
    in_flight = getattr(runtime, "agent_boot_in_flight", None)
    if callable(in_flight) and in_flight(WELCOME_AGENT) is True:
        return True
    boot = getattr(runtime, "_boot_agent", None)
    if not callable(boot):
        return False
    if _paused_by_owner():
        logger.info("welcome greeting: %s is paused by the owner; not starting it", WELCOME_AGENT)
        return False
    logger.info("welcome greeting: %s is not running — starting it", WELCOME_AGENT)
    try:
        await boot(WELCOME_AGENT)
    except Exception:
        # _boot_agent parks its own failures on the registry; anything that
        # escapes is logged here and surfaces as a missing runner below.
        logger.warning("welcome greeting: starting %s failed", WELCOME_AGENT, exc_info=True)
    return True


def _write_label(runner: Any, thread_id: str) -> None:
    """Title the rail row before the first row lands.

    Mirrors what ``PATCH /api/agents/{name}/threads/{id}`` writes
    (``api._thread_meta_path``): ``<thread>.meta.json`` beside the transcript,
    ``{"name": …}``. Derived from the runner's own session path so the two can
    never point at different directories.
    """
    try:
        session = runner._session_path(thread_id)
        meta_path = session.with_name(f"{thread_id}.meta.json")
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta: dict[str, Any] = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        if not meta.get("name"):
            meta["name"] = THREAD_LABEL
            meta_path.write_text(json.dumps(meta), encoding="utf-8")
    except Exception:  # a missing title is cosmetic
        logger.debug("welcome thread label not written", exc_info=True)


async def greet_new_owner(
    runtime: Any,
    user: Any,
    thread_id: str,
    *,
    wait_seconds: float = RUNNER_WAIT_SECONDS,
    poll: float = RUNNER_POLL_SECONDS,
) -> dict[str, Any] | None:
    """Run the one greeting turn. Never raises (see module doc)."""
    try:
        runner = (getattr(runtime, "_runners", None) or {}).get(WELCOME_AGENT)
        if runner is None:
            await _start_agent_father(runtime)
            runner = await _wait_for_runner(runtime, wait_seconds, poll)
        if runner is None:
            logger.warning(
                "welcome greeting skipped: %s runner not up within %ss",
                WELCOME_AGENT,
                wait_seconds,
            )
            # No runner means no session path to write a note into; the
            # dashboard's own backstop timer ends the wait in this case.
            return None
        session = runner._session_path(thread_id)
        if session.exists() and session.stat().st_size > 0:
            logger.info("welcome greeting skipped: thread %s already has rows", thread_id)
            return {"thread_id": thread_id, "skipped": "exists"}
        _write_label(runner, thread_id)
        user_id = str(getattr(user, "id", "") or "owner")
        with push_request_context(
            # "api" → the dashboard channel in the rail (thread_labels), which is
            # where the owner reads it. The scheduled origin is what lets Agent
            # Father's HITL gates auto-resolve: no human is present to approve,
            # and the prompt forbids changes anyway.
            source="api",
            channel="api",
            user_id=user_id,
            session_id=thread_id,
            authenticated=True,
            is_owner=True,
            is_admin=True,
            metadata={
                ORIGIN_KEY: make_origin(
                    "scheduled", False, root_user_id=user_id, root_session_id=thread_id
                ),
                HIDDEN_PROMPT_KEY: True,
            },
        ):
            try:
                reply = await runner.ainvoke(build_greeting_prompt(user), thread_id=thread_id)
            except Exception as exc:
                logger.exception("welcome greeting failed")
                _write_failure_note(runner, thread_id, exc)
                return None
        logger.info("welcome greeting delivered on %s/%s", WELCOME_AGENT, thread_id)
        return {"thread_id": thread_id, "reply": reply}
    except Exception:
        logger.exception("welcome greeting failed")
        return None


def _write_failure_note(runner: Any, thread_id: str, exc: Exception) -> None:
    """Put the reason a greeting failed in the thread the owner is looking at.

    The dashboard holds the welcome thread on "waking up" until a visible row
    lands (``ChatView``'s ``waking``), and a failed turn wrote none: the hidden
    prompt row is never drawn, so the owner sat on the spinner for the whole
    six-minute backstop and then met the same error on their first message
    (a new managed box, 2026-10-26, with no credit grant yet). One
    assistant row with the classified, user-facing message ends the wait the
    moment it fails (``_append_session`` publishes ``thread_updated``) and
    says why. Never a traceback; never raises.
    """
    try:
        from olano.core.actionable_errors import to_error_frame

        frame = to_error_frame(exc)
        message = " ".join(str(frame.get("message") or "").split())
        if not message:
            return
        runner._append_session(
            thread_id,
            "assistant",
            f"I couldn't say hello just now. {message}",
            extra={"error_code": str(frame.get("code") or "")},
        )
        runner.record_turn_error(thread_id, message, code=str(frame.get("code") or ""))
    except Exception:  # a missing note costs the explanation, nothing else
        logger.debug("welcome greeting failure note not written", exc_info=True)
