"""Multi-user scheduling: per-customer fan-out and customer-owned jobs.

Two job types, both only reachable on a multi-user agent
(``memory.per_customer``); a normal agent's scheduling path never enters this
module.

**Type 1 — ``for_each_customer`` config jobs.** A schedule entry in
``agent.json5`` runs once per customer folder under the agent's
``workspace/customers/`` tree (the folder list *is* the user list). Per
customer it either runs an ``allowed_actions`` script — deterministic, no
model turn, identity in the environment, exactly the ``execute_action``
contract — or runs one agent turn with that customer's identity bound. The
canonical use is "once a day, run the subscription check for every user".

**Type 2 — customer-owned DB tasks.** An end-user asks the agent for their own
reminder; the identity-injected tools (``tools/customer_scheduler.py``) store
the task with THEIR ``platform`` + raw ``user_id`` (resolved from the
authenticated request context, never a tool argument). When it fires, the run
binds that identity — so ``active_customer_id()`` resolves the real customer
instead of refusing under the scheduler's synthetic channel — and the turn's
reply is delivered back to them on their own platform.

IDENTITY RULES (same as everywhere else in multi-user mode)
-----------------------------------------------------------
* The **raw** sender id is the delivery address; the **sanitized** spelling
  (``sanitize_identity``) is the folder/store key. They differ for any
  rewritten id, and conflating them silently addresses nobody.
* Fan-out starts from folders, not messages, so the raw address comes from the
  ``.customer.json`` sidecar each live contact writes (falling back to the
  folder name, correct for already-safe ids).

ENTITLEMENT
-----------
* A Type-2 job checks the owning customer's subscription **at fire time** (it
  can lapse between firings): a lapsed user's job is skipped with a history
  note, and resumes by itself if they resubscribe.
* A Type-1 **action** run is NOT gated — the flagship use case is the
  subscription check itself, which must see lapsed users.
* A Type-1 **agent-turn** run on a monetized agent skips lapsed users: each
  turn costs the owner tokens, and pushing paid content to expired users is
  exactly what ``notification_send_to_subscribers`` exists to prevent.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from olano.scheduler import clock
from olano.scheduler.cron import fresh_run_thread_id

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig, ScheduleConfig

logger = logging.getLogger(__name__)

#: Reply a scheduled customer turn uses to mean "send nothing". Digest-style
#: jobs are quiet-unless-actionable — an empty ritual message is how a
#: personal alert turns back into the group chat it replaced — but a model
#: cannot reliably produce an empty reply, so silence gets an explicit
#: spelling. Compared after stripping; the sentinel itself is never delivered.
SILENCE_SENTINEL = "NO_MESSAGE"

#: Prefix for every customer-bound scheduled turn. Mirrors the normal DB-task
#: preamble (no human to ask), plus the delivery contract so the model writes
#: the reply *to the customer*, not a status report to the void — and the
#: silence contract so "nothing worth sending" never arrives as a message.
CUSTOMER_TURN_PREAMBLE = (
    "[Autonomous scheduled task for the customer you serve — execute directly "
    "and completely. Do NOT ask for confirmation or approval before acting. "
    "Your final reply will be delivered to this customer on their own chat; "
    "reply with exactly NO_MESSAGE if there is nothing worth sending them.]\n\n"
)


class CustomerTaskSkipped(Exception):
    """This firing must not run (e.g. the owning customer's subscription lapsed).

    Not a failure: the task stays enabled and re-checks on its next fire, so a
    resubscribed customer's reminders resume on their own.
    """


def customer_task_identity(task: dict[str, Any]) -> tuple[str, str] | None:
    """``(platform, raw_user_id)`` when a DB task is customer-owned, else None.

    Both columns must be set — a half-set identity is treated as absent rather
    than guessed at, so a malformed row degrades to the normal (owner) path
    where the phantom-identity guard refuses per-customer tools anyway.
    """
    platform = str(task.get("platform") or "").strip()
    user_id = str(task.get("user_id") or "").strip()
    if platform and user_id:
        return platform, user_id
    return None


async def deliver_text(agent_name: str, platform: str, user_id: str, text: str) -> bool:
    """Send *text* to one end-user on their platform. Best-effort, never raises.

    The target is the RAW sender id (what the platform adapter addresses),
    which is exactly what Type-2 rows store and ``.customer.json`` records.
    """
    # Delivery boundary check: muted (customer's own switch) or blocked
    # (operator's switch) suppresses every scheduled send, whatever path
    # produced it — the fan-out and turn paths skip earlier and cheaper, this
    # is the belt-and-suspenders for any future caller. Best-effort: an
    # unreadable prefs file must not eat a legitimate delivery.
    try:
        from olano.memory.customer_memory import customer_key_for, customers_base
        from olano.memory.customer_prefs import delivery_suppressed

        key = customer_key_for(platform, user_id)
        reason = delivery_suppressed(customers_base(agent_name) / key) if key else None
        if reason:
            logger.info(
                "Suppressed scheduled delivery to %s/%s for agent '%s' (%s)",
                platform,
                user_id,
                agent_name,
                reason,
            )
            return False
    except Exception:
        logger.debug("Delivery-preference check failed; delivering", exc_info=True)

    try:
        from olano.platforms import MessageEnvelope, get_channel_registry

        registry = get_channel_registry()
        if registry.get(agent_name, platform) is None:
            logger.warning(
                "Cannot deliver scheduled result: agent '%s' has no '%s' channel",
                agent_name,
                platform,
            )
            return False
        envelope = MessageEnvelope(
            text=text, target=user_id, channel_type=platform, agent_name=agent_name
        )
        result = await registry.send(envelope, raise_on_error=True)
        return result is not None
    except Exception as exc:
        logger.warning(
            "Scheduled delivery to %s/%s failed for agent '%s': %s",
            platform,
            user_id,
            agent_name,
            exc,
        )
        return False


def _entitlement_blocked(agent_name: str, platform: str, config: Any) -> str | None:
    """Reason the CURRENT context's user may not receive paid work, or None.

    Wraps the monetization gate (trial windows, grace periods, expiry — one
    rule-set, not a re-implementation). Must be called inside the customer's
    pushed request context, because the gate resolves the user from it.
    Fail-open on gate errors: a broken billing lookup must not silently stop
    every scheduled job.
    """
    if not getattr(getattr(config, "monetization", None), "enabled", False):
        return None
    try:
        from olano.monetization.gate import evaluate_access

        decision = evaluate_access(agent_name, platform, config)
        if not decision.allowed:
            return decision.reason or "expired"
    except Exception:
        logger.warning("Entitlement check failed for agent '%s'; allowing", agent_name)
    return None


async def _bound_customer_turn(
    runner: Any,
    agent_name: str,
    platform: str,
    user_id: str,
    prompt: str,
    thread_id: str,
    *,
    extra_metadata: dict[str, Any] | None = None,
    check_entitlement: bool = False,
    tz_name: str | None = None,
) -> str:
    """Run ONE agent turn as one identified customer and deliver the reply.

    The pushed context is the whole point: inside it ``active_customer_id()``
    is the real user (channel = their platform, not ``"scheduler"``), so every
    identity-injected tool behaves exactly as in a live chat — and only this
    one user's data can enter this one model context. ``is_owner=False``
    because the customer is not the operator.

    Raises :class:`CustomerTaskSkipped` when ``check_entitlement`` finds the
    subscription lapsed.
    """
    from olano.security.request_context import (
        ORIGIN_KEY,
        make_origin,
        push_request_context,
    )

    config = getattr(runner, "config", None)
    metadata: dict[str, Any] = {
        ORIGIN_KEY: make_origin(
            "scheduled", False, root_user_id=user_id, root_session_id=thread_id
        ),
        **(extra_metadata or {}),
    }
    with push_request_context(
        source="scheduled",
        channel=platform,
        user_id=user_id,
        session_id=thread_id,
        authenticated=True,
        is_owner=False,
        metadata=metadata,
    ):
        # Mute/block gate, resolved exactly like the identity-injected tools
        # resolve their store — this covers Type-2 tasks and any turn-mode
        # caller in one place (Type-1 fan-out also pre-filters on the folder,
        # so a muted customer costs no model turn there).
        try:
            from olano.memory.customer_memory import active_customer_id, customers_base
            from olano.memory.customer_prefs import delivery_suppressed

            cid = active_customer_id()
            reason = delivery_suppressed(customers_base(agent_name) / cid) if cid else None
        except Exception:
            logger.debug("Mute/block check failed; running turn", exc_info=True)
            reason = None
        if reason:
            raise CustomerTaskSkipped(f"skipped: notifications {reason}")
        if check_entitlement and config is not None:
            reason = _entitlement_blocked(agent_name, platform, config)
            if reason is not None:
                raise CustomerTaskSkipped(f"skipped: subscription inactive ({reason})")
        # The customer preamble plus the current date and time in the task's
        # timezone (scheduler/clock), same as every other autonomous run.
        stamped = clock.autonomous_preamble(
            prompt, tz_name=tz_name, directive=CUSTOMER_TURN_PREAMBLE
        )
        response = await runner.ainvoke(stamped, thread_id=thread_id)

    text = str(response).strip()
    if text == SILENCE_SENTINEL:
        # The turn ran and decided there is nothing worth sending — the
        # sentinel itself must never reach the customer.
        return ""
    if text:
        delivered = await deliver_text(agent_name, platform, user_id, text)
        if not delivered:
            logger.warning(
                "Scheduled turn for %s/%s on '%s' completed but the reply was not delivered",
                platform,
                user_id,
                agent_name,
            )
    return text


async def run_customer_task_turn(
    runner: Any,
    agent_name: str,
    platform: str,
    user_id: str,
    prompt: str,
    thread_id: str,
    task_id: str,
    *,
    tz_name: str | None = None,
) -> str:
    """Fire one Type-2 (customer-owned) DB task: gate, bind, run, deliver."""
    return await _bound_customer_turn(
        runner,
        agent_name,
        platform,
        user_id,
        prompt,
        thread_id,
        extra_metadata={"task_id": task_id},
        check_entitlement=True,
        tz_name=tz_name,
    )


# ─────────────────────────────── Type-1 fan-out ──────────────────────────────


def _fan_out_action_script(config: AgentConfig, action: str) -> Path | None:
    """Resolve a schedule entry's ``action`` name to its script file.

    Same resolution the builder uses for ``execute_action`` (workspace, the
    agent's own directory, configured skill roots), so an action behaves
    identically whether a model or the scheduler invokes it.
    """
    from olano.config.home import get_olano_paths
    from olano.tools.customer_actions import resolve_action_script

    script_ref = (config.allowed_actions or {}).get(action)
    if not script_ref:
        return None
    roots = [
        Path(config.workspace) if config.workspace else Path.cwd(),
        get_olano_paths().agent_workspace_dir(config.name).parent,
        *(Path(d) for d in (config.skills or [])),
    ]
    return resolve_action_script(script_ref, roots)


#: Placeholders an operator may write into a ``for_each_customer`` entry's
#: ``message``; each is replaced per customer before the bound turn runs. This
#: exists for owner-keyed tools (e.g. the kaki MCP's ``kaki_matches(owner=…)``,
#: keyed by the ``<platform>-<id>`` folder name): the turn's model has no other
#: trustworthy way to learn which customer it is serving — asking it to infer
#: one would reopen the exact forgeable-identity hole this design closes.
#: Plain ``str.replace``, not ``str.format``: any other brace in operator prose
#: must pass through untouched.
MESSAGE_PLACEHOLDERS = ("{customer_key}", "{platform}", "{user_id}")


def render_customer_message(
    template: str, *, customer_key: str, platform: str, user_id: str
) -> str:
    """Substitute the per-customer placeholders into a fan-out turn message."""
    return (
        template.replace("{customer_key}", customer_key)
        .replace("{platform}", platform)
        .replace("{user_id}", user_id)
    )


def _notify_from_stdout(stdout: str) -> str | None:
    """The optional per-customer message an action's JSON result carries.

    Contract: a top-level ``{"notify": "<text>"}`` string asks the runtime to
    deliver that text to the customer the action just ran for. Anything else —
    non-JSON output, other keys, empty string — is data for the logs, not a
    message, so a chatty script can never accidentally spam its user.
    """
    out = (stdout or "").strip()
    if not out:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    if isinstance(data, dict):
        notify = data.get("notify")
        if isinstance(notify, str) and notify.strip():
            return notify.strip()
    return None


async def run_for_each_customer(
    job_id: str,
    config: AgentConfig,
    sched: ScheduleConfig,
    runner: Any,
) -> str:
    """Execute one ``for_each_customer`` firing across every customer folder.

    Customers run **sequentially** with per-customer failure isolation: one
    user's broken ledger or dead chat must never touch their neighbours, and a
    burst of N parallel turns/sends would trip platform flood limits anyway.
    Returns the summary line that is also recorded in task history.
    """
    from olano.memory.customer_memory import customers_base, load_customer_identity
    from olano.tools.customer_actions import (
        DEFAULT_ACTION_TIMEOUT,
        run_customer_action,
    )

    agent_name = config.name
    base = customers_base(agent_name)
    folders = (
        sorted((p for p in base.iterdir() if p.is_dir()), key=lambda p: p.name)
        if base.is_dir()
        else []
    )
    if not folders:
        summary = "fan-out: no customers yet"
        logger.info("Scheduled job '%s': %s", job_id, summary)
        return summary

    script: Path | None = None
    if sched.action:
        script = _fan_out_action_script(config, sched.action)
        if script is None:
            summary = (
                f"fan-out: action {sched.action!r} does not resolve to a script "
                "in allowed_actions — nothing ran"
            )
            logger.error("Scheduled job '%s': %s", job_id, summary)
            return summary

    ran = notified = failed = skipped = 0
    for folder in folders:
        identity = load_customer_identity(folder)
        if identity is None:
            skipped += 1
            logger.warning(
                "Scheduled job '%s': cannot resolve identity for %s — skipped",
                job_id,
                folder.name,
            )
            continue
        platform, user_id = identity
        # The customer's own mute switch or the operator's block marker: skip
        # before any work happens — a muted customer costs no action run and
        # no model turn. (deliver_text re-checks as the last line of defense.)
        from olano.memory.customer_prefs import delivery_suppressed

        suppressed = delivery_suppressed(folder)
        if suppressed:
            skipped += 1
            logger.info(
                "Scheduled job '%s': customer %s is %s — skipped",
                job_id,
                folder.name,
                suppressed,
            )
            continue
        try:
            if script is not None:
                from olano.tools.customer_credentials import customer_secret_env

                proc = await asyncio.to_thread(
                    run_customer_action,
                    script,
                    {},
                    customer_dir=folder,
                    platform=platform,
                    user_id=user_id,
                    customer_key=folder.name,
                    timeout=DEFAULT_ACTION_TIMEOUT,
                    extra_env=customer_secret_env(agent_name, folder.name),
                )
                if proc.returncode != 0:
                    failed += 1
                    logger.error(
                        "Scheduled job '%s': action %r failed for %s (exit %s): %s",
                        job_id,
                        sched.action,
                        folder.name,
                        proc.returncode,
                        (proc.stderr or proc.stdout or "").strip()[:500],
                    )
                    continue
                ran += 1
                notify = _notify_from_stdout(proc.stdout)
                if notify and await deliver_text(agent_name, platform, user_id, notify):
                    notified += 1
            else:
                thread_id = fresh_run_thread_id(f"scheduled_{agent_name}_{folder.name}")
                message = render_customer_message(
                    sched.message,
                    customer_key=folder.name,
                    platform=platform,
                    user_id=user_id,
                )
                try:
                    await _bound_customer_turn(
                        runner,
                        agent_name,
                        platform,
                        user_id,
                        message,
                        thread_id,
                        extra_metadata={"config_job_id": job_id},
                        check_entitlement=True,
                        tz_name=(getattr(sched, "timezone", None) or "").strip() or None,
                    )
                    ran += 1
                except CustomerTaskSkipped:
                    skipped += 1
                    continue
        except Exception as exc:
            failed += 1
            logger.error("Scheduled job '%s' failed for customer %s: %s", job_id, folder.name, exc)
            continue

    summary = f"fan-out: {ran} ran, {notified} notified, {skipped} skipped, {failed} failed"
    logger.info("Scheduled job '%s' completed — %s", job_id, summary)
    return summary


# ─────────────────────────────── erasure hook ────────────────────────────────


def cancel_active_customer_tasks(agent_name: str) -> int:
    """Delete every scheduled job the CURRENT customer owns on this agent.

    Called by ``forget_me`` — erasure covers queued future work, not just
    stored data. Identity comes from the request context, so it can only ever
    remove the asking customer's own rows; the live APScheduler triggers are
    dropped too so nothing fires between now and the next restart.
    """
    from olano.memory.customer_memory import active_customer_platform
    from olano.security.request_context import get_request_context

    platform = active_customer_platform()
    user_id = (get_request_context().user_id or "").strip()
    if not platform or not user_id:
        return 0

    from olano.scheduler.db import SchedulerDB

    ids = SchedulerDB().delete_user_tasks(agent_name, platform, user_id)
    if not ids:
        return 0
    try:
        from olano.scheduler.cron import get_active_scheduler

        scheduler = get_active_scheduler()
        if scheduler is not None:
            for task_id in ids:
                scheduler.remove_db_task(task_id)
    except Exception:
        logger.debug("Could not drop live triggers for erased tasks", exc_info=True)
    try:
        from olano.runtime.api import _push_event

        for task_id in ids:
            _push_event("scheduler_update", {"action": "deleted", "id": task_id})
    except Exception:
        pass
    return len(ids)
