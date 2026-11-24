"""Group-thread tools — agents as first-class participants in group chats.

Until these existed a bot could only *answer* inside a group: a human posted,
the addressee replied, and (with ``groups.autonomous`` on) mentions carried the
turn onward. Nothing let a bot walk up to a group and speak — post a finding
into the war room, hand a task to a teammate where the human can watch — which
is half the point of having groups at all.

Four tools over ``olano/groups/store.py``. The acting agent binds from the
``current_agent`` contextvar, never from a parameter — an agent cannot post as
somebody else — and every tool answers ``[tool-error]`` prose rather than
raising, because the caller is a model mid-turn, not a stack trace reader.

Three rules keep this from becoming a side door:

- **Feature-gated like the HTTP surface.** Everything checks
  ``groups.enabled`` first; the tools exist in the index either way, but on a
  deployment with groups off they refuse with the switch's name, exactly as
  the routes 404.
- **Membership is the permission to speak.** ``group_send`` requires the
  acting agent to be on the roster. The roster is a *speaking* permission
  only: follow-on turns re-check per hop against the group's **creator** —
  the human who assembled the roster — through the same rule the human path
  uses, so a grant revoked after the group formed still stops the next turn.
- **A bot post follows the same autonomy contract as a human post.** If the
  message addresses another member and ``groups.autonomous`` is on, the
  bounded cascade runs (hop cap, wall clock, ping-pong guard — all of it);
  with autonomy off the message simply sits in the transcript. ``group_send``
  never runs another agent's turn inline — that is what the cascade, with its
  budgets, is for, and ``talk_to_agent`` already covers direct A2A.

Thread identity is the subtle part: the cascade runs each member on the thread
``group-<id>-<agent>``, namespaced **by the group's creator** when the creator
is a plain member (``auth.namespace_thread_id`` does this from the request
context on the human path). An agent-initiated turn has no human request
context, so ``_thread_id_for`` reproduces the creator's prefix explicitly —
otherwise the same group would run its members on two disjoint thread ids
depending on who spoke first, forking every bot's memory of the conversation.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from langchain_core.tools import tool

from olano.groups.store import GroupError, get_group_store
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)


def _me() -> str:
    from olano.tools.talk_to import current_agent

    return current_agent.get() or ""


def _settings() -> Any:
    from olano.config.system_settings import load_group_thread_settings

    return load_group_thread_settings()


def _disabled() -> str | None:
    try:
        if not _settings().enabled:
            return (
                "[tool-error] Group threads are turned off on this deployment "
                "(config.yaml `groups.enabled`). An admin can enable them."
            )
    except Exception:  # pragma: no cover - unreadable config reads as off
        return "[tool-error] Group threads are unavailable (settings unreadable)."
    return None


def _publish(group_id: int, kind: str, **extra: Any) -> None:
    """Content-free ``group_updated`` poke, same contract as the routes."""
    try:
        from olano.runtime.groups_routes import _publish as poke

        poke(group_id, kind, **extra)
    except Exception:  # pragma: no cover - CLI / tests without a runtime
        pass


def _my_groups(me: str) -> list[dict]:
    store = get_group_store()
    # limit=500 is the store's own ceiling — a deployment with more groups
    # than that has bigger problems, but an agent must never be told a group
    # it belongs to does not exist because it fell off a recency page.
    return [g for g in store.list_groups(limit=500) if me in (g.get("members") or [])]


def _find_group(me: str, group_ref: str) -> dict | str:
    """Resolve an id or exact name to a group this agent belongs to."""
    mine = _my_groups(me)
    ref = str(group_ref).strip()
    for g in mine:
        if str(g["id"]) == ref or g["name"] == ref:
            return g
    names = ", ".join(f"#{g['id']} {g['name']}" for g in mine) or "none"
    return (
        f"[tool-error] No group '{group_ref}' with you in it. "
        f"Your groups: {names}. (Groups you are not a member of are not listed "
        "and cannot be posted to.)"
    )


def _creator_user(created_by: str) -> Any:
    """The human whose grants govern this group's follow-on turns.

    ``owner`` (and any single-user context) is the implicit admin; anyone else
    must still exist and be active — a group whose creator was deleted or
    deactivated must not keep speaking on their authority.
    """
    from olano.runtime import auth as auth_layer

    if not auth_layer.multi_user_enabled() or created_by in ("", "owner", "anonymous"):
        return auth_layer._OWNER
    from olano.users.store import get_user_store

    user = get_user_store().db.get_user_by_id(created_by)
    if user is None or not user.is_active:
        return None
    return user


def _creator_access_check(created_by: str):
    """Per-hop access check bound to the creator, mirroring the human path.

    The human path passes ``auth.assert_agent_access`` (bound to the request's
    user). Here there is no request user, so the same rule is asked of the
    creator explicitly: admins reach everything, members only what they were
    granted.
    """
    from olano.users.store import get_user_store

    def check(agent: str) -> None:
        user = _creator_user(created_by)
        if user is None:
            raise GroupError(f"group creator '{created_by}' is gone; no turns can run")
        if user.is_admin:
            return
        if not get_user_store().can_chat(user, agent):
            raise GroupError(f"the group's creator may no longer reach '{agent}'")

    return check


def _thread_id_for(group: dict):
    """Creator-namespaced thread ids — the ONE definition, shared with routes.

    This began as a local copy that got the rule right while the HTTP path
    got it wrong (it namespaced by the *poster*, forking member memory the
    first time an admin posted into a member's group). The fix moved the rule
    to ``groups_routes.group_thread_id`` and left this as a delegation, so
    the two paths can never disagree again.
    """
    from olano.runtime.groups_routes import group_thread_id

    def thread_id(name: str) -> str:
        return group_thread_id(group, name)

    return thread_id


def _maybe_cascade_from_tool(group: dict, *, speaker: str, text: str) -> str:
    """Run the addressed member's turn(s) through the bounded cascade.

    Returns a short suffix for the tool's reply describing what will happen —
    the model should know whether its message starts turns or just sits.
    """
    from olano.groups.cascade import REGISTRY, CascadeLimits, next_speaker, run_cascade
    from olano.tools import talk_to

    settings = _settings()
    if not getattr(settings, "autonomous", False):
        return " Autonomous follow-on turns are off, so members will see it but not reply on their own."

    handed_to = next_speaker(text, speaker=speaker, members=group.get("members") or [])
    if not handed_to:
        return ""

    runtime = talk_to._RUNTIME
    if runtime is None:  # CLI / tests without a bound runtime
        return " (No runtime bound — the message is posted but no member turn can run here.)"

    # Resolve where the cascade can run BEFORE claiming the group. In
    # production a sync @tool executes in a worker thread under the agent
    # graph — there is no running loop in this thread, and the first version
    # here called asyncio.get_running_loop() after REGISTRY.begin(): the
    # RuntimeError path returned without finish(), leaving the group's
    # cascade slot claimed forever (task=None never reads as finished). So:
    # find the loop first, claim second, and every path after begin() either
    # schedules or releases.
    try:
        this_loop: Any = asyncio.get_running_loop()
    except RuntimeError:
        this_loop = None
    target_loop = this_loop
    if target_loop is None:
        try:
            from olano.platforms import get_channel_registry

            candidate = get_channel_registry().loop
            if candidate is not None and candidate.is_running():
                target_loop = candidate
        except Exception:
            target_loop = None
    if target_loop is None:
        return " (No event loop — the message is posted but no member turn can run here.)"

    limits = CascadeLimits.from_settings(settings)
    state = REGISTRY.begin(group["id"], limits=limits)
    if state is None:
        return (
            " A conversation is already running in this group; your message joins the transcript."
        )

    def _runner_for(name: str) -> Any:
        return getattr(runtime, "_runners", {}).get(name)

    creator_check = _creator_access_check(str(group.get("created_by") or ""))

    def _access(agent: str) -> None:
        creator_check(agent)

    def _brief(g: dict, name: str) -> str:
        from olano.runtime.groups_routes import briefing_for

        return briefing_for(runtime, settings, g, name)

    kwargs = dict(
        group_id=group["id"],
        store=get_group_store(),
        limits=limits,
        runner_for=_runner_for,
        assert_access=_access,
        thread_id_for=_thread_id_for(group),
        publish=lambda kind, **extra: _publish(group["id"], kind, **extra),
        # Same standing rules the HTTP path injects, from the one builder —
        # a member must not hear a different account of the room depending on
        # whether a human or a bot started the exchange.
        briefing_for=_brief,
        speaker=speaker,
        reply=text,
    )

    async def _drive() -> None:
        try:
            await run_cascade(state=state, **kwargs)
        except asyncio.CancelledError:
            pass
        except Exception:  # pragma: no cover - run_cascade classifies its own failures
            logger.exception("group %s: tool-initiated cascade failed", group["id"])
        finally:
            REGISTRY.finish(state)

    try:
        if this_loop is target_loop and this_loop is not None:
            # Called with a loop in this thread (async tool adapters, tests).
            state.task = target_loop.create_task(_drive())
        else:
            # Worker thread: hand the coroutine to the runtime's loop. The
            # returned concurrent Future supports cancel()/done(), which is
            # all the registry and /stop ask of a task.
            state.task = asyncio.run_coroutine_threadsafe(_drive(), target_loop)
    except Exception:
        # Scheduling failed — release the group or every future cascade in it
        # (human path included) answers "busy" until a process restart.
        REGISTRY.finish(state)
        logger.exception("group %s: could not schedule tool-initiated cascade", group["id"])
        return " (Could not start the follow-on turn — the message is posted.)"
    _publish(group["id"], "cascade_start", max_hops=limits.max_hops)
    return f" @{handed_to} will take the next turn (up to {limits.max_hops} follow-on turns)."


@tool
def group_list() -> str:
    """List the group chats you are a member of, with their members.

    Groups are shared conversations between two and six bots that a human can
    watch. You can only see and post to groups you belong to; a human adds you
    from the dashboard.
    """
    err = _disabled()
    if err:
        return err
    me = _me()
    if not me:
        return "[tool-error] No acting agent bound."
    mine = _my_groups(me)
    if not mine:
        return "You are not in any group chats yet. A human can add you to one from the dashboard."
    lines = [
        f"#{g['id']} {g['name']} — members: {', '.join(g.get('members') or [])}"
        f" ({g.get('message_count', 0)} messages)"
        for g in mine
    ]
    return "\n".join(lines)


@tool
def group_read(group: str, limit: int = 20) -> str:
    """Read the latest messages in one of your group chats.

    ``group`` is the group's id (e.g. ``3``) or exact name. Returns up to
    ``limit`` recent messages, oldest first, each attributed as
    ``author -> addressee``. Use this to catch up before posting.
    """
    err = _disabled()
    if err:
        return err
    me = _me()
    if not me:
        return "[tool-error] No acting agent bound."
    found = _find_group(me, group)
    if isinstance(found, str):
        return found
    limit = max(1, min(int(limit), 100))
    msgs = get_group_store().messages(found["id"], limit=limit)
    if not msgs:
        return f"#{found['id']} {found['name']} has no messages yet."
    lines = []
    for m in msgs[-limit:]:
        arrow = f" -> {m['addressee']}" if m.get("addressee") else ""
        lines.append(f"[{m.get('author_kind', '?')}] {m['author']}{arrow}: {m['body']}")
    return "\n".join(lines)


@tool
def group_send(group: str, text: str, to: str = "") -> str:
    """Post a message into one of your group chats.

    ``group`` is the group's id or exact name — you must be a member.
    ``text`` is what to say; mention a member with ``@name`` to address them.
    ``to`` (optional) addresses a specific member explicitly and wins over
    mentions.

    Whether the addressed member replies on its own depends on the
    deployment's autonomy setting; either way your message is saved and every
    human watching the group sees it immediately. Your own name is stamped as
    the author — you cannot post as anyone else.
    """
    err = _disabled()
    if err:
        return err
    me = _me()
    if not me:
        return "[tool-error] No acting agent bound."
    body = (text or "").strip()
    if not body:
        return "[tool-error] Nothing to send — `text` is empty."
    found = _find_group(me, group)
    if isinstance(found, str):
        return found
    members = found.get("members") or []
    addressee = (to or "").strip()
    if addressee and addressee not in members:
        return (
            f"[tool-error] '{addressee}' is not in #{found['id']} {found['name']}. "
            f"Members: {', '.join(members)}."
        )
    if addressee == me:
        return "[tool-error] You cannot address yourself."
    try:
        get_group_store().add_message(
            found["id"], author=me, author_kind="agent", body=body, addressee=addressee or ""
        )
    except GroupError as exc:
        return f"[tool-error] {exc}"
    # No author on the poke: group_updated frames ride the shared bus and
    # must stay content-free — a subscriber with no access to this group's
    # agents must not learn which agent just spoke.
    _publish(found["id"], "message")
    # `to` wins over mentions, matching the human POST: fold it into the text
    # handed to the turn-taking rule only when explicit.
    effective = f"@{addressee} {body}" if addressee else body
    suffix = _maybe_cascade_from_tool(found, speaker=me, text=effective)
    return f"Posted to #{found['id']} {found['name']}.{suffix}"


@tool
def group_create(name: str, members: str) -> str:
    """Create a new group chat for the human you are working with.

    ``name`` is the group's title; ``members`` is a comma-separated list of
    2-6 bot names — include yourself if you mean to take part (e.g.
    ``"merli, sales-outbound"``). The group belongs to the human in this
    conversation: they see it in their sidebar and can rename, extend or
    delete it. Only works when a signed-in human is present — from a
    scheduled or background run, ask a human to create the group instead.
    """
    err = _disabled()
    if err:
        return err
    me = _me()
    if not me:
        return "[tool-error] No acting agent bound."

    from olano.runtime import auth as auth_layer

    user = None
    try:
        user = auth_layer.current_user()
    except Exception:
        user = None
    if user is None:
        return (
            "[tool-error] No signed-in human in this conversation, so there is nobody "
            "to own the group. Ask the human to create it from their sidebar."
        )

    roster = [m.strip() for m in (members or "").split(",") if m.strip()]
    settings = _settings()
    # The same per-member rule the HTTP route applies: the owner-to-be must be
    # able to reach every member. Admins reach everything.
    if not user.is_admin:
        from olano.users.store import get_user_store

        blocked = [m for m in roster if not get_user_store().can_chat(user, m)]
        if blocked:
            return (
                f"[tool-error] {user.name or 'This user'} does not have access to: "
                f"{', '.join(blocked)}. A group can only hold bots its owner can reach."
            )
    try:
        group = get_group_store().create(
            name=(name or "").strip() or "New group",
            members=roster,
            created_by=user.id,
            min_members=getattr(settings, "min_members", 2),
            max_members=getattr(settings, "max_members", 6),
        )
    except GroupError as exc:
        return f"[tool-error] {exc}"
    _publish(group["id"], "created")
    return (
        f"Created group #{group['id']} '{group['name']}' with "
        f"{', '.join(group.get('members') or [])}. It is in the human's sidebar now."
    )


get_tools, get_tool = make_toolkit_api(globals())
