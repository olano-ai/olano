"""Dashboard routes for multi-bot group threads (``/api/groups*``).

A group is a named roster of 2–6 agents plus an attributed transcript
(``olano/groups/store.py``). Posting to one addresses **the members the human
named** — every ``@name`` in the message, in the order they were written, else
the group's first member — and hands each of them a turn on the same
:class:`~olano.core.runner.AgentRunner` the dashboard chat uses. Each member in
that fan-out sees what the ones before it said, so "@a @b @c look into this"
produces three complementary answers rather than three copies of one. There is
deliberately no second agent loop here: a group turn is an ordinary turn on a
group-scoped thread id, so checkpointing, credentials, stream isolation and
cost accounting behave exactly as they do in a 1:1 chat.

Every turn is prefixed with the same **briefing** (``cascade.group_briefing``):
who is in the room and what each of them does, what ``@name`` means in both
directions, and which agent-to-agent tools *this* member actually holds. That
prefix is why a member can decide to pull a teammate in rather than guess at
the shape of the room, and building it in one place is why the human path and
the cascade can never tell two members different stories about it.

With ``groups.autonomous`` on (a **second** switch, also on by default), that
one reply may hand the conversation to another member by ``@name`` and the agents
continue between themselves — bounded by ``groups.max_hops`` and
``groups.cascade_timeout_seconds``, and stoppable at any moment. The bounds and
the loop live in ``olano/groups/cascade.py``; this module only wires them to a
runtime, an identity and the SSE bus.

**Both switches ship on, and both are honoured live.** The routes are
registered unconditionally (so the app's shape never depends on config) but
every one of them answers 404 while ``groups.enabled`` is off in
``~/.olano/config.yaml`` — an install that turns it off cannot tell this
module exists, and turning it back on brings every group and transcript back
untouched.

Two guards, neither of which this module invents:

* **Roster** — every member must be an agent the caller can already reach
  (``auth.assert_agent_access``). Grouping agents can never be a way to talk to
  one you were not granted.
* **Visibility** — a group belongs to the human who created it (their stable
  user id); admins see every group; and the creator (or an admin) may **share**
  it with other team accounts (``group_shares``), who then read and post in it.
  Single-user installs resolve to the implicit owner, who is an admin, so
  nothing is hidden there.

Sharing is the document model, not a roster change. Three rules keep it from
becoming a side door: a person can only be shared into a group whose every
agent they already hold a grant on (and an agent can only be added to a shared
group when every shared person holds one on it); every post is still checked
against the *poster's* grants, exactly as before; and a shared person reads,
posts and leaves, while renaming, re-rostering, re-sharing and deleting stay
with the creator and admins (``_require_manage``). The one behavioural change
a share makes to the room is turn-taking: with more than one person in it, a
message that names no agent is for the other people and wakes nobody
(``pick_addressees(require_mention=True)``), because "thanks Maria, I'll take
it" must not cost a model call.

Registered from ``runtime/api.py:create_api`` next to the issues routes, so the
central security middleware applies to it.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request

from olano.core.runs import REGISTRY as _RUNS
from olano.core.runs import AgentRun
from olano.core.runs import sse_run_view as _sse_run_view
from olano.groups.cascade import (
    MENTION_RE,
    REGISTRY,
    CascadeLimits,
    CascadeState,
    group_briefing,
    next_speaker,
    render_transcript,
    run_cascade,
)
from olano.groups.seed import build_carry_over, carry_over_note, read_session_rows
from olano.groups.segments import SegmentAccumulator
from olano.groups.store import GroupError, get_group_store
from olano.runtime.media_refs import SCAN_TOOL_RESULTS
from olano.security.request_context import AGENT_ADMIN_KEY, push_request_context

logger = logging.getLogger(__name__)

#: The ``cascade.reason`` a turn reports when the message named no agent in a
#: room with more than one person in it: saved for the people, nobody woken.
NOBODY_ADDRESSED = "nobody_addressed"

#: The ``agent`` slot of a group run's registry key. A group turn has as many
#: agents as it has speakers, so the conversation is the group id and this is
#: the fixed namespace it lives in — the key stays ``(kind, agent, thread)``
#: for every surface rather than growing a per-surface shape.
_GROUP_RUN_AGENT = "__group__"


def _session_file(agent: str, thread_id: str) -> Path:
    """The agent's JSONL transcript for a thread, preferring plain over gzipped.

    Same resolution the dashboard's own thread reader uses — housekeeping
    compresses old sessions, and a promoted conversation must carry whether or
    not that has happened to it yet.
    """
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / agent
    plain = sessions / f"{thread_id}.jsonl"
    return plain if plain.exists() else sessions / f"{thread_id}.jsonl.gz"


def _settings() -> Any:
    from olano.config.system_settings import load_group_thread_settings

    return load_group_thread_settings()


def _require_enabled() -> Any:
    """The ``groups:`` settings, or 404 when the feature is off.

    404 rather than 403 on purpose: while the switch is off this deployment has
    no group surface at all, and saying "forbidden" would advertise one.
    """
    settings = _settings()
    if not settings.enabled:
        raise HTTPException(status_code=404, detail="Not Found")
    return settings


def _current_user() -> Any:
    from olano.runtime.auth import current_user

    user = current_user()
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return user


def _actor(user: Any) -> str:
    """The posting human as a canonical principal (``human-<slug>``).

    Attribution in the transcript, never identity: visibility is decided on
    ``created_by``, which holds the stable user id, so renaming yourself
    changes how your old messages are labelled and nothing else.
    """
    from olano.issues.principals import HUMAN_PREFIX, human_principal

    try:
        # username first: it is the permanent handle human-<username> is built
        # from (issues_routes does the same). Falling back through name keeps
        # rows written before the handle existed attributable; building from
        # name alone was the bug — a display-name edit re-attributed every past
        # group turn and broke the viewer-relative `mine` stamping with it.
        return human_principal(
            getattr(user, "username", "") or getattr(user, "name", "") or "owner"
        )
    except Exception:  # pragma: no cover - defensive
        return f"{HUMAN_PREFIX}owner"


def _assert_roster_access(members: list[str]) -> None:
    """Every member must be an agent this human may already chat with."""
    from olano.runtime.auth import assert_agent_access

    for agent in members:
        assert_agent_access(agent)


def _visible(group: dict, user: Any) -> bool:
    """May this human open the group: admin, its creator, or shared into it."""
    if bool(getattr(user, "is_admin", False)):
        return True
    uid = getattr(user, "id", None)
    return group.get("created_by") == uid or uid in (group.get("shared_with") or [])


def _can_manage(group: dict, user: Any) -> bool:
    """May this human change the group itself (name, roster, shares, delete).

    Deliberately narrower than :func:`_visible`: a person who was shared into
    a group is a guest in it. They read, post and leave; the room belongs to
    whoever made it, and to admins.
    """
    return bool(getattr(user, "is_admin", False)) or group.get("created_by") == getattr(
        user, "id", None
    )


def _require_visible(group_id: int, user: Any) -> dict:
    group = get_group_store().get(group_id)
    if group is None or not _visible(group, user):
        # A group another human owns is indistinguishable from one that does
        # not exist — an existence oracle over other people's threads would be
        # a leak in itself.
        raise HTTPException(status_code=404, detail=f"No group #{group_id}")
    return group


def _require_manage(group_id: int, user: Any) -> dict:
    """The group, if this human may change it; 404 if they cannot see it at
    all, 403 if they can only take part in it."""
    group = _require_visible(group_id, user)
    if not _can_manage(group, user):
        raise HTTPException(status_code=403, detail="group_manage_denied")
    return group


def _team_lookup() -> tuple[dict[str, Any], dict[str, Any]]:
    """``(users by id, users by username)``, every team account, read once.

    Empty on a single-user install: there is no users table to read, and the
    implicit owner is synthesized where it is needed (:func:`group_people`).
    """
    from olano.runtime import auth as auth_layer

    if not auth_layer.multi_user_enabled():
        return {}, {}
    from olano.users.store import get_user_store

    users = get_user_store().db.list_users()
    return {u.id: u for u in users}, {u.username: u for u in users if u.username}


def _person(user: Any, *, principal: str, role: str) -> dict[str, Any]:
    return {
        "id": str(getattr(user, "id", "") or ""),
        "username": str(getattr(user, "username", "") or ""),
        "name": str(getattr(user, "name", "") or ""),
        "principal": principal,
        "role": role,
        "is_admin": bool(getattr(user, "is_admin", False)),
    }


def group_people(
    group: dict,
    *,
    by_id: dict[str, Any] | None = None,
    by_username: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Everyone in the room who is not a bot, in a stable order.

    The creator first, then the people shared into it (oldest share first),
    then anyone else the transcript has heard from, which can only be an
    admin, since nobody else can post without being one of the first two.
    Each entry carries the ``human-<handle>`` principal the transcript labels
    that person with, so the dashboard's People section and the agents'
    briefing read off one list and can never name a person two ways.

    One list, two readers: ``briefing_for`` hands it to every member on every
    turn, and the routes attach it to the group they return.
    """
    from olano.issues.principals import bare_name, kind
    from olano.runtime import auth as auth_layer

    if by_id is None or by_username is None:
        by_id, by_username = _team_lookup()
    people: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(user: Any, principal: str, role: str) -> None:
        if principal in seen:
            return
        seen.add(principal)
        people.append(_person(user, principal=principal, role=role))

    created_by = str(group.get("created_by") or "")
    creator = by_id.get(created_by)
    if creator is not None:
        add(creator, _actor(creator), "creator")
    elif not by_id or created_by in ("", "owner", "anonymous"):
        # Single-user install, or a group made before team accounts were on:
        # the implicit owner, whose transcript rows read `human-owner`.
        add(auth_layer._OWNER, _actor(auth_layer._OWNER), "creator")
    else:
        # The creator's account is gone. Still the creator, their turns are
        # in the transcript under their handle, but there is no name to show.
        people.append(
            {
                "id": created_by,
                "username": "",
                "name": "",
                "principal": "",
                "role": "creator",
                "is_admin": False,
            }
        )
    for uid in group.get("shared_with") or []:
        user = by_id.get(str(uid))
        if user is not None:
            add(user, _actor(user), "shared")
    for principal in group.get("humans") or []:
        if kind(principal) != "human":
            continue
        user = by_username.get(bare_name(principal))
        if user is None:
            # Spoke, but matches no account: a renamed-away row from before
            # usernames existed, or a deleted account. Listed by handle only.
            if principal not in seen:
                seen.add(principal)
                people.append(
                    {
                        "id": "",
                        "username": bare_name(principal),
                        "name": "",
                        "principal": principal,
                        "role": "spoke",
                        "is_admin": False,
                    }
                )
            continue
        add(user, principal, "spoke")
    return people


def _with_people(group: dict, lookup: tuple[dict, dict] | None = None) -> dict:
    """The group as the API returns it: the store's row plus ``people``."""
    by_id, by_username = lookup if lookup is not None else _team_lookup()
    group["people"] = group_people(group, by_id=by_id, by_username=by_username)
    return group


def _resolve_team_account(ref: str) -> Any:
    """A team account by id, username or email; 404 when there is none.

    The three spellings because the dashboard sends the id it was given by
    the candidates list, while an agent relaying "share it with maria" has
    only a handle, and refusing the handle would send it back to ask for an
    id nobody knows.
    """
    from olano.users.store import get_user_store

    wanted = str(ref or "").strip()
    if not wanted:
        raise HTTPException(status_code=400, detail="A team account is required.")
    db = get_user_store().db
    user = db.get_user_by_id(wanted)
    if user is None:
        lowered = wanted.lower()
        for candidate in db.list_users():
            if candidate.username.lower() == lowered or candidate.email.lower() == lowered:
                user = candidate
                break
    if user is None:
        raise HTTPException(status_code=404, detail="No such team account.")
    return user


def _shares_lacking_access(group: dict, agent: str) -> list[str]:
    """Display names of the people shared into ``group`` who cannot reach
    ``agent``, the ones a roster change would hand an agent they were never
    granted."""
    from olano.users.store import get_user_store

    by_id, _ = _team_lookup()
    store = get_user_store()
    blocked: list[str] = []
    for uid in group.get("shared_with") or []:
        user = by_id.get(str(uid))
        if user is not None and not store.can_chat(user, agent):
            blocked.append(user.name or user.username or user.email)
    return blocked


def pick_addressees(
    text: str,
    members: list[str],
    explicit: str = "",
    *,
    require_mention: bool = False,
) -> list[str]:
    """Every member this message addresses, in the order it named them.

    ``explicit`` (the request's ``to`` field) still wins and still names
    exactly one member: a picker in the composer is a choice of one, and
    letting it fan out would make the dropdown mean something different from
    what it says. Otherwise the list is every ``@name`` in the text that
    matches a member — because "@marcus @aurelius @curtis can you research X"
    is one request to three bots, and answering it with one reply from the
    first of them is the single most confusing thing a group chat can do.
    A message that names nobody falls back to the group's first member, which
    is why roster order is preserved on write.

    ``require_mention`` turns that fallback off and returns ``[]`` instead:
    the rule for a room with more than one person in it, where a message that
    names no bot is for the other people. The caller persists it and wakes
    nobody. Single-person rooms never pass it, so their behaviour is unchanged.

    De-duplicated and case-insensitive, so ``@Beta @beta`` is one turn for
    ``beta`` and never two.
    """
    by_lower = {m.lower(): m for m in members}
    chosen = (explicit or "").strip().lower()
    if chosen and chosen in by_lower:
        return [by_lower[chosen]]
    if chosen:
        raise HTTPException(status_code=400, detail=f"'{explicit}' is not in this group.")
    named: list[str] = []
    for mention in MENTION_RE.findall(text or ""):
        member = by_lower.get(mention.lower())
        if member and member not in named:
            named.append(member)
    if named or require_mention:
        return named
    return [members[0]]


def pick_addressee(text: str, members: list[str], explicit: str = "") -> str:
    """The member who answers FIRST — the head of :func:`pick_addressees`.

    Kept as its own name because it is what a single-addressee caller means
    (the ``addressee`` field of a response, the client's typing line), and
    because deriving it here is what stops the two rules from drifting.
    """
    return pick_addressees(text, members, explicit)[0]


def _member_roster(runtime: Any, group: dict) -> list[dict]:
    """One entry per member: name, its own description, whether it is running.

    The description is the same sentence the human sees beside that bot in the
    sidebar, and it is the whole reason a member can decide *who* to pull in
    rather than just *that* it may. Read off the live runner, so an agent that
    is not booted is reported honestly rather than silently promised.
    """
    runners = getattr(runtime, "_runners", {}) or {}
    roster: list[dict] = []
    for name in group.get("members") or []:
        runner = runners.get(name)
        cfg = getattr(runner, "config", None)
        description = getattr(cfg, "description", "")
        roster.append(
            {
                # Anything that is not a string is not a description — never
                # stringify it into the prompt, which is what a stray mock or a
                # half-built config would otherwise put in front of the model.
                "name": name,
                "description": description if isinstance(description, str) else "",
                "online": runner is not None,
            }
        )
    return roster


#: Tools that let a member reach another agent. Only these are named in a
#: turn's briefing, and only when the member actually holds them.
PEER_TOOL_NAMES = ("talk_to_agent", "group_send", "group_read", "group_list")


def _member_peer_tools(runtime: Any, agent: str) -> tuple[list[str], list[str]]:
    """``(peer tools this member holds, its subagent names)``.

    Read from the merged ``AgentConfig`` rather than from
    ``resolve_effective_tool_names``: this runs on every group turn and that
    helper probes provider credentials for the multimodal defaults, none of
    which can be a way to talk to another agent. ``disabled_tools`` still
    subtracts, because a tool an operator turned off must not be advertised.
    """
    runner = (getattr(runtime, "_runners", {}) or {}).get(agent)
    cfg = getattr(runner, "config", None)
    if cfg is None:
        return [], []
    configured = set(getattr(cfg, "tools", None) or [])
    disabled = set(getattr(cfg, "disabled_tools", None) or [])
    held = [t for t in PEER_TOOL_NAMES if t in configured and t not in disabled]
    subs: list[str] = []
    if "task" not in disabled:
        for sub in getattr(cfg, "subagents", None) or []:
            name = getattr(sub, "name", None) or (sub.get("name") if isinstance(sub, dict) else "")
            if name:
                subs.append(str(name))
    return held, subs


def briefing_for(runtime: Any, settings: Any, group: dict, agent: str) -> str:
    """The standing rules of this group, as ``agent`` needs to hear them.

    One builder for both paths — the human turn below and every cascade hop —
    because two members disagreeing about what ``@name`` means is exactly the
    failure a group chat cannot survive.
    """
    peer_tools, subagents = _member_peer_tools(runtime, agent)
    try:
        humans = group_people(group)
    except Exception:  # pragma: no cover - a users DB hiccup must not cost the turn
        logger.debug("group %s: people lookup failed", group.get("id"), exc_info=True)
        humans = []
    return group_briefing(
        group,
        agent,
        roster=_member_roster(runtime, group),
        peer_tools=peer_tools,
        subagents=subagents,
        autonomous=bool(getattr(settings, "autonomous", False)),
        humans=humans,
    )


def compose_turn(
    group: dict,
    agent: str,
    unseen: list[dict],
    *,
    briefing: str = "",
    also_addressed: list[str] | None = None,
) -> str:
    """The prompt handed to ``agent`` for its turn.

    Only the messages it has not been shown yet (``GroupStore.unseen_for``)
    travel in — everything before its own last message is already in its
    checkpointed thread, and re-sending it would both cost tokens and make the
    model see the conversation twice.

    ``briefing`` is :func:`group_briefing` (who is here, what ``@name`` means,
    which tools reach the others); ``also_addressed`` names the other members
    the same human message called on, so a fan-out turn knows it is doing one
    part of a shared request instead of all of it. Both default to empty, so
    the old two-argument call still produces a working prompt.
    """
    if not briefing:
        others = [m for m in group["members"] if m != agent] or ["nobody yet"]
        briefing = (
            f'You are "{agent}" in the group chat "{group["name"]}". '
            f"The other members are: {', '.join(others)}."
        )
    others_named = [m for m in (also_addressed or []) if m != agent]
    if others_named:
        shared = (
            "This message also names "
            + ", ".join(f"@{m}" for m in others_named)
            + " — they take their own turns on it. Do the part that is yours, "
            "and leave theirs to them."
        )
    else:
        shared = "You are the one being addressed."
    return (
        f"{briefing}\n\n"
        f"{shared} Reply in your own voice — do not narrate the other members "
        "or answer on their behalf. Mention a member by @name if you want them "
        "to take the next turn.\n\n"
        f"New messages since your last turn:\n{render_transcript(unseen)}"
    )


#: ``astream_events`` kinds a group transcript carries. ``text`` is the reply
#: itself; the other three are the work behind it — which in a group is half of
#: what there is to watch, since a member reaching a peer or a subagent IS the
#: collaboration. Everything else (``usage``, ``approval``, ``thread_switch``)
#: belongs to a private 1:1 turn and is dropped: a group has no approval UI,
#: and per-turn cost belongs on the dashboard, not in a shared transcript.
STREAMED_EVENT_KINDS = ("text", "tool_call", "tool_result", "subagent_text")


def _stream_frame(kind: str, payload: Any, author: str) -> dict[str, Any] | None:
    """One ``astream_events`` tuple as a wire frame, or ``None`` to drop it.

    Every frame names its ``author``: a group turn can involve several members
    in sequence, and a client that cannot tell whose tool call it is watching
    will draw the second member's work under the first member's face.
    """
    if kind not in STREAMED_EVENT_KINDS:
        return None
    frame: dict[str, Any] = {"type": kind, "author": author}
    if isinstance(payload, str):
        if not payload:
            return None
        frame["text"] = payload
    elif isinstance(payload, dict):
        frame.update(payload)
    else:  # pragma: no cover - the runner yields only str/dict payloads
        return None
    # Namespaces arrive as tuples, which json cannot serialize.
    ns = frame.get("namespace")
    if isinstance(ns, tuple):
        frame["namespace"] = list(ns)
    return frame


def _member_media(text: str, agent: str, roots: dict[str, Any]) -> list[dict[str, Any]]:
    """Files ``agent`` named, resolved against ``agent``'s OWN workspace.

    A group has as many workspace roots as it has members, and
    ``detect_media_refs`` stamps ``files_url``/``download_url`` with the agent it
    is handed — so one root per group would attribute beta's screenshot to alpha
    and serve it from the wrong workspace. ``roots`` memoizes the lookup for the
    turn: resolving a root parses that agent's ``agent.json5``, and a six-member
    fan-out would otherwise pay for it on every tool result.
    """
    if not text.strip():
        return []
    if agent not in roots:
        try:
            from olano.runtime.api import _workspace_root

            roots[agent] = _workspace_root(agent)
        except Exception:
            logger.debug("group media: no workspace for %s", agent, exc_info=True)
            roots[agent] = None
    root = roots.get(agent)
    if root is None:
        return []
    try:
        from olano.runtime.media_refs import detect_media_refs

        return detect_media_refs(text, root, agent)
    except Exception:
        logger.debug("group media detection skipped for %s", agent, exc_info=True)
        return []


def _cascade_runs_inline() -> bool:
    """Run the cascade inside the request instead of behind it.

    Off in production; ``LIONCLAW_SYNC_GROUP_CASCADE=1`` makes the POST await
    the whole exchange. Same escape hatch as ``LIONCLAW_SYNC_CARTOGRAPHY`` /
    ``LIONCLAW_SYNC_ISSUE_EMBED``: a background task is not observable from a
    synchronous test client, and a feature whose *failure modes* are the point
    has to be testable end to end.
    """
    return os.environ.get("LIONCLAW_SYNC_GROUP_CASCADE", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def group_thread_id(group: dict, name: str) -> str:
    """The thread a group member runs on — named after the group's CREATOR.

    Namespacing by the *poster* (what ``auth.namespace_thread_id`` used to do
    from the request context) was the bug: groups are visible to their creator
    and to admins, so the first time an admin posted into a member's group the
    addressed agent ran on a fresh ``u:<admin>:``-prefixed thread with none of
    the conversation — every member's memory forked by whoever happened to
    speak. The creator is the one stable identity a group has, so the thread
    key derives from ``created_by`` alone, exactly as the agent-tool path
    (``group_tools._thread_id_for``) already did; this helper is the one
    definition both share. The ``u:<creator>:`` prefix is a naming scheme only
    now — thread visibility follows agent access — and stays so existing
    group threads keep their ids.
    """
    from olano.runtime import auth as auth_layer

    created_by = str(group.get("created_by") or "")
    prefix = ""
    if auth_layer.multi_user_enabled() and created_by not in ("", "owner", "anonymous"):
        prefix = auth_layer._thread_prefix(created_by)
    return f"{prefix}group-{group['id']}-{name}"


def _cascade_kwargs(runtime: Any, settings: Any, group_id: int) -> dict[str, Any]:
    """Everything ``run_cascade`` needs from this process, resolved once.

    The per-hop access check is the *same function* the human path calls, not a
    copy of its rule: a mention only earns a turn if the human who started the
    exchange may still reach that agent.
    """
    from olano.runtime import auth as auth_layer

    def _runner_for(name: str) -> Any:
        return getattr(runtime, "_runners", {}).get(name)

    cascade_group = get_group_store().get(group_id) or {"id": group_id, "created_by": ""}

    def _thread_id_for(name: str) -> str:
        return group_thread_id(cascade_group, name)

    def _poke(kind: str, **extra: Any) -> None:
        _publish(group_id, kind, **extra)

    def _brief(group: dict, name: str) -> str:
        return briefing_for(runtime, settings, group, name)

    return {
        "group_id": group_id,
        "store": get_group_store(),
        "limits": CascadeLimits.from_settings(settings),
        "runner_for": _runner_for,
        "assert_access": auth_layer.assert_agent_access,
        "thread_id_for": _thread_id_for,
        "publish": _poke,
        # A hop hears the same standing rules as a human-addressed turn: who
        # is in the room, what @name means, and which tools reach the others.
        "briefing_for": _brief,
    }


async def _drive_cascade(state: CascadeState, kwargs: dict[str, Any]) -> None:
    """Background wrapper: run the cascade, always release the group."""
    try:
        await run_cascade(state=state, **kwargs)
    except asyncio.CancelledError:  # a human pressed Stop, or the loop is going down
        pass
    except Exception:  # pragma: no cover - run_cascade classifies its own failures
        logger.exception("group %s: cascade driver failed", state.group_id)
    finally:
        REGISTRY.finish(state)


async def _maybe_cascade(
    settings: Any,
    runtime: Any,
    *,
    group_id: int,
    speaker: str,
    reply: str,
    handed_to: str,
) -> dict[str, Any]:
    """Start the bot-to-bot exchange this reply asks for, if any.

    **The POST returns after the first reply and the cascade continues behind
    it.** A cascade is bounded in hops but not in wall-clock feel — several
    model turns can run for minutes — so blocking the human's request on it
    would trade a prompt answer for a spinner, and a client that navigates away
    would take the exchange with it. Instead every hop is persisted as it lands
    and pokes ``/api/stream``, so a live dashboard renders the conversation as
    it happens and a client that only polls
    ``GET /api/groups/{id}/messages`` still sees the identical, complete
    transcript. ``LIONCLAW_SYNC_GROUP_CASCADE=1`` inverts this for tests.
    """
    if not getattr(settings, "autonomous", False):
        return {"running": False, "reason": "off"}
    if not handed_to:
        return {"running": False, "reason": "settled"}
    limits = CascadeLimits.from_settings(settings)
    state = REGISTRY.begin(group_id, limits=limits)
    if state is None:
        # One ordered conversation per group: a second cascade would interleave
        # two exchanges into one transcript. The human's message was still
        # answered — only the follow-on turns are declined.
        return {"running": False, "reason": "busy"}
    kwargs = _cascade_kwargs(runtime, settings, group_id)
    kwargs.update(speaker=speaker, reply=reply)
    if _cascade_runs_inline():
        try:
            result = await run_cascade(state=state, **kwargs)
        finally:
            REGISTRY.finish(state)
        return {"running": False, **result.as_dict()}
    task = asyncio.create_task(_drive_cascade(state, kwargs))
    # Held by the registry (which owns the only reference), so the task is not
    # garbage-collected mid-flight and ``/stop`` has something to cancel.
    state.task = task
    _publish(group_id, "cascade_start", max_hops=limits.max_hops)
    return {"running": True, "hops": 0, "max_hops": limits.max_hops}


def register_groups_routes(app: Any, runtime: Any) -> None:
    """Attach ``/api/groups*``. Registered always; 404s while the flag is off."""

    @app.get("/api/groups")
    async def list_groups() -> dict:
        _require_enabled()
        user = _current_user()
        # Admins list everything; everyone else the groups they made or were
        # shared into. One team lookup serves every row's `people`.
        visible_to = None if getattr(user, "is_admin", False) else getattr(user, "id", None)

        def _read() -> list[dict]:
            lookup = _team_lookup()
            rows = get_group_store().list_groups(visible_to=visible_to)
            return [_with_people(g, lookup) for g in rows]

        return {"groups": await asyncio.to_thread(_read)}

    @app.post("/api/groups")
    async def create_group(body: dict) -> dict:
        settings = _require_enabled()
        user = _current_user()
        members = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
        # Access first: a caller must not learn from the error message whether
        # a roster size was wrong for an agent they cannot see anyway.
        _assert_roster_access(members)
        try:
            group = await asyncio.to_thread(
                lambda: get_group_store().create(
                    name=str(body.get("name") or ""),
                    members=members,
                    created_by=str(getattr(user, "id", "owner")),
                    min_members=settings.min_members,
                    max_members=settings.max_members,
                )
            )
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "create")
        return {"group": _with_people(group)}

    @app.post("/api/groups/from-thread")
    async def create_group_from_thread(body: dict) -> dict:
        """Promote a one-to-one conversation into a group.

        The bot you were already talking to leads the new roster, which is not
        cosmetic: members[0] answers anything that names nobody, so the next
        message you send goes where it would have gone anyway and adding a
        second bot never silently redirects the conversation.

        Both guards from the plain create still apply, plus one this route
        needs on its own: the thread has to be visible to the caller. Without
        it, naming someone else's thread id would copy their conversation into
        a group of your own.
        """
        settings = _require_enabled()
        user = _current_user()
        agent = str(body.get("agent") or "").strip()
        thread_id = str(body.get("thread_id") or "").strip()
        if not agent:
            raise HTTPException(status_code=400, detail="An agent is required.")
        extra = [str(m).strip() for m in (body.get("members") or []) if str(m).strip()]
        # The leading bot is the conversation's own; de-duplicated so picking
        # it again in the dialog cannot produce a roster with it twice.
        members = [agent] + [m for m in extra if m != agent]
        _assert_roster_access(members)

        from olano.runtime.auth import thread_visible_to_current_user

        if thread_id and not thread_visible_to_current_user(thread_id):
            raise HTTPException(status_code=404, detail="Thread not found")

        carry = bool(body.get("carry_over", True)) and bool(thread_id)
        # Naming is optional here, unlike the plain create. This route backs a
        # one-click "add a bot to this chat", and stopping to name a room is
        # exactly the friction that makes people re-explain themselves in the
        # 1:1 instead. The roster is a good enough label and stays renameable.
        name = str(body.get("name") or "").strip() or " & ".join(members)

        def _build() -> dict:
            group = get_group_store().create(
                name=name,
                members=members,
                created_by=str(getattr(user, "id", "owner")),
                min_members=settings.min_members,
                max_members=settings.max_members,
            )
            if not carry:
                return group
            # Seeding is best-effort on purpose: the group is already real and
            # usable, so an unreadable or vanished session file must cost the
            # history, never the group the human just asked for.
            store = get_group_store()
            written = 0
            total = 0
            try:
                rows = read_session_rows(_session_file(agent, thread_id))
                total = sum(1 for r in rows if r.get("role") in ("user", "assistant"))
                for row in build_carry_over(rows, agent=agent, human=_actor(user)):
                    # Spelled out rather than `**row`: `add_message` also takes
                    # `segments`, which is not a string, so a spread of the
                    # carry-over's `dict[str, str]` no longer type-checks — and
                    # a carried turn has no spans to carry anyway (the 1:1
                    # transcript's tool cards do not come along).
                    store.add_message(
                        group["id"],
                        author=row["author"],
                        author_kind=row["author_kind"],
                        body=row["body"],
                    )
                    written += 1
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("group seed from thread %s failed: %s", thread_id, e)
            # Counted from what actually landed, not what was planned, and
            # written outside the try — a copy that died halfway still has to
            # say it is partial. A history that passes itself off as complete
            # is the one failure this whole path cannot afford.
            if written:
                try:
                    store.add_message(
                        group["id"],
                        author="system",
                        author_kind="system",
                        body=carry_over_note(agent, written, total),
                    )
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning("group seed note for thread %s failed: %s", thread_id, e)
                group = store.require(group["id"])
            return group

        try:
            group = await asyncio.to_thread(_build)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "create")
        return {"group": _with_people(group)}

    @app.get("/api/groups/{group_id}")
    async def get_group(group_id: int) -> dict:
        _require_enabled()
        user = _current_user()
        return {
            "group": await asyncio.to_thread(lambda: _with_people(_require_visible(group_id, user)))
        }

    @app.patch("/api/groups/{group_id}")
    async def rename_group(group_id: int, body: dict) -> dict:
        """Rename a group. The only field a group has that is safe to edit.

        Members move through the roster routes (they carry access checks) and
        the transcript is append-only, so this handler accepts ``name`` and
        nothing else — an unknown key is ignored rather than half-applied.
        """
        _require_enabled()
        user = _current_user()
        name = str(body.get("name") or "").strip()

        def _rename() -> dict:
            _require_manage(group_id, user)
            return _with_people(get_group_store().rename(group_id, name))

        try:
            group = await asyncio.to_thread(_rename)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "renamed")
        return {"group": group}

    @app.delete("/api/groups/{group_id}")
    async def delete_group(group_id: int) -> dict:
        _require_enabled()
        user = _current_user()

        def _delete() -> dict:
            group = _require_manage(group_id, user)
            get_group_store().delete(group_id)
            return group

        group = await asyncio.to_thread(_delete)
        _publish(group, "delete")
        return {"ok": True, "id": group_id}

    @app.post("/api/groups/{group_id}/members")
    async def add_group_member(group_id: int, body: dict) -> dict:
        settings = _require_enabled()
        user = _current_user()
        agent = str(body.get("agent") or "").strip()
        _assert_roster_access([agent] if agent else [])

        def _add() -> dict:
            group = _require_manage(group_id, user)
            # The share rule, kept from the other side: a person is only ever
            # shared into a group whose agents they can all reach, so an agent
            # cannot be added over the head of somebody who cannot reach it.
            blocked = _shares_lacking_access(group, agent) if agent else []
            if blocked:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{', '.join(blocked)} can read this group but cannot use '{agent}'. "
                        "Grant them access to it first, or remove them from the group."
                    ),
                )
            return _with_people(
                get_group_store().add_member(group_id, agent, max_members=settings.max_members)
            )

        try:
            group = await asyncio.to_thread(_add)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "member_added")
        return {"group": group}

    @app.delete("/api/groups/{group_id}/members/{agent}")
    async def remove_group_member(group_id: int, agent: str) -> dict:
        settings = _require_enabled()
        user = _current_user()

        def _remove() -> dict:
            _require_manage(group_id, user)
            return _with_people(
                get_group_store().remove_member(group_id, agent, min_members=settings.min_members)
            )

        try:
            group = await asyncio.to_thread(_remove)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "member_removed")
        return {"group": group}

    @app.get("/api/groups/{group_id}/share-candidates")
    async def group_share_candidates(group_id: int) -> dict:
        """The team accounts this group could be shared with.

        Active, not an admin (admins already open every group), not the
        creator, not already in, and holding a grant on **every** agent in the
        roster, the same rule the share itself enforces, so the picker never
        offers a name the server would refuse. Only somebody who may share
        gets the list: a guest has no use for it, and a members' directory
        is not something the group surface should hand out.
        """
        _require_enabled()
        user = _current_user()

        def _read() -> list[dict]:
            group = _require_manage(group_id, user)
            from olano.runtime import auth as auth_layer

            if not auth_layer.multi_user_enabled():
                return []
            from olano.users.store import get_user_store

            store = get_user_store()
            taken = set(group.get("shared_with") or []) | {str(group.get("created_by") or "")}
            out: list[dict] = []
            for candidate in store.db.list_users():
                if not candidate.is_active or candidate.is_admin or candidate.id in taken:
                    continue
                if all(store.can_chat(candidate, m) for m in group.get("members") or []):
                    out.append(
                        {
                            "id": candidate.id,
                            "username": candidate.username,
                            "name": candidate.name,
                        }
                    )
            return out

        return {"candidates": await asyncio.to_thread(_read)}

    @app.post("/api/groups/{group_id}/shares")
    async def share_group(group_id: int, body: dict) -> dict:
        """Let a team account read and post in this group.

        ``user`` is an id, a username or an email. Refused for an admin (they
        already see it), for the creator (it is theirs), and for anyone who
        does not hold a grant on every agent in the roster, a share must
        never be the way somebody reaches an agent they were not given.
        """
        _require_enabled()
        user = _current_user()
        from olano.runtime import auth as auth_layer

        if not auth_layer.multi_user_enabled():
            raise HTTPException(
                status_code=400,
                detail="Team accounts are off on this deployment, so there is nobody to share with.",
            )
        ref = str(body.get("user") or "").strip()

        def _share() -> dict:
            group = _require_manage(group_id, user)
            target = _resolve_team_account(ref)
            if not target.is_active:
                raise HTTPException(status_code=400, detail="That account is disabled.")
            if target.is_admin:
                raise HTTPException(
                    status_code=400, detail="Admins already see every group; nothing to share."
                )
            if target.id == str(group.get("created_by") or ""):
                raise HTTPException(status_code=400, detail="That person created this group.")
            from olano.users.store import get_user_store

            store = get_user_store()
            blocked = [m for m in group.get("members") or [] if not store.can_chat(target, m)]
            if blocked:
                who = target.name or target.username or target.email
                raise HTTPException(
                    status_code=400,
                    detail=(
                        f"{who} does not have access to {', '.join(blocked)}. Grant it on the "
                        "Admin page first, or share a group that only holds agents they can use."
                    ),
                )
            return _with_people(get_group_store().share(group_id, target.id, added_by=user.id))

        try:
            group = await asyncio.to_thread(_share)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "shared")
        return {"group": group}

    @app.delete("/api/groups/{group_id}/shares/{user_id}")
    async def unshare_group(group_id: int, user_id: str) -> dict:
        """Take a person back out, or let them leave.

        The creator and admins remove anyone; a shared person may remove
        themselves (that is "Leave"), and nobody else. What they posted stays
        in the transcript under their name.
        """
        _require_enabled()
        user = _current_user()

        def _unshare() -> dict:
            group = _require_visible(group_id, user)
            leaving_self = str(user_id) == str(getattr(user, "id", ""))
            if not leaving_self and not _can_manage(group, user):
                raise HTTPException(status_code=403, detail="group_manage_denied")
            return _with_people(get_group_store().unshare(group_id, user_id))

        try:
            group = await asyncio.to_thread(_unshare)
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        _publish(group, "unshared")
        return {"group": group}

    @app.get("/api/groups/{group_id}/messages")
    async def list_group_messages(group_id: int, limit: int = 200) -> dict:
        _require_enabled()
        user = _current_user()

        def _read() -> dict:
            group = _with_people(_require_visible(group_id, user))
            # ``mine`` is viewer-relative, so it is stamped per request rather
            # than stored: the same row is "You" to its author and their name
            # to everyone else — an admin reading a member's group must never
            # see that member's turns on their own side of the column.
            me = _actor(user)
            rows = get_group_store().messages(group_id, limit=limit)
            # Media is re-derived per read rather than stored, exactly as the
            # 1:1 transcript does (`api._transcript_message`): a file the agent
            # has since deleted stops being offered instead of leaving a card
            # that 404s. Roots are memoized across the whole read — a six-member
            # group would otherwise re-parse each `agent.json5` per row.
            roots: dict[str, Any] = {}
            for row in rows:
                row["mine"] = row.get("author_kind") == "human" and row.get("author") == me
                refs = _member_media(
                    str(row.get("body") or ""), str(row.get("author") or ""), roots
                )
                if refs:
                    row["media"] = refs
            return {"group": group, "messages": rows}

        return await asyncio.to_thread(_read)

    async def _begin_group_turn(group_id: int, body: dict) -> dict:
        """Everything both message POSTs share, up to "the human turn is saved".

        One function on purpose: the blocking and streaming endpoints must
        agree on validation, addressing, access and persistence order, and two
        copies of thirty lines is how they would stop agreeing. The human turn
        is persisted *before* the agent runs, so a model that errors or times
        out costs the reply and never the message that was actually sent.

        A message may name **several** members ("@a @b look at this"), and each
        named member then takes its own turn in the order it was named. Access
        and ownership are therefore resolved per member here, while the session
        is still in hand — the streaming generator below runs after the
        middleware's context is gone.
        """
        settings = _require_enabled()
        user = _current_user()
        text = str(body.get("text") or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="A message needs text.")

        store = get_group_store()
        group = await asyncio.to_thread(lambda: _require_visible(group_id, user))
        members: list[str] = group["members"]
        if not members:  # pragma: no cover - the store refuses an empty roster
            raise HTTPException(status_code=400, detail="This group has no members.")
        # A shared group holds more than one person, and there a message that
        # names no agent is for the other people: it is saved, and wakes nobody.
        named = pick_addressees(
            text,
            members,
            str(body.get("to") or ""),
            require_mention=bool(group.get("shared_with")),
        )

        # Every addressed agent must still be one this human may talk to: a
        # grant can be revoked after the group was formed, and the roster is
        # not a standing permission.
        from olano.runtime import auth as auth_layer

        runners = getattr(runtime, "_runners", {}) or {}
        agent_admin: dict[str, bool] = {}
        speakers: list[str] = []
        for candidate in named:
            auth_layer.assert_agent_access(candidate)
            # Whether this human may ADMINISTER that member — the self_config
            # ownership gate reads it from the turn context (AGENT_ADMIN_KEY).
            # Resolved here, where the user and the agent name are both in
            # hand, for the same reason /api/chat/stream resolves it before its
            # generator: the streaming turn below runs on the fail-open default
            # the gate must never trust.
            agent_admin[candidate] = auth_layer.can_admin_agent(user, candidate)
            if candidate in runners:
                speakers.append(candidate)
        if named and not speakers:
            # Nothing that was named can actually answer. Said in terms of the
            # first name the human wrote, which is the one they will look for.
            raise HTTPException(status_code=404, detail=f"Agent '{named[0]}' is not running.")

        actor = _actor(user)
        try:
            human_msg = await asyncio.to_thread(
                lambda: store.add_message(
                    group_id,
                    author=actor,
                    author_kind="human",
                    body=text,
                    # Every member the message called on, so the transcript's
                    # own "→" says who was asked rather than naming one of
                    # three and leaving the other two looking uninvited.
                    addressee=", ".join(named),
                )
            )
        except GroupError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        human_msg["mine"] = True  # this copy goes back to the human who sent it
        _publish(group, "message")

        return {
            "settings": settings,
            "group": group,
            "members": members,
            # `addressee` stays the single member that answers first: it is
            # what the response field and the client's typing line mean. Empty
            # when the message was for the other people in the room.
            "addressee": speakers[0] if speakers else "",
            "addressees": speakers,
            "named": named,
            "agent_admin": agent_admin,
            "human_msg": human_msg,
        }

    async def _member_turn(turn: dict, agent: str) -> dict:
        """The prompt + thread for one member's turn, resolved when it speaks.

        Deliberately not precomputed with the rest of the turn: in a fan-out
        the second member must see what the first one just said, and
        ``unseen_for`` can only answer that once the first reply is in the
        store.
        """
        store = get_group_store()
        group = turn["group"]
        unseen = await asyncio.to_thread(lambda: store.unseen_for(group["id"], agent))
        brief = briefing_for(runtime, turn["settings"], group, agent)
        return {
            "agent": agent,
            "runner": (getattr(runtime, "_runners", {}) or {}).get(agent),
            "agent_admin": bool(turn["agent_admin"].get(agent, False)),
            "prompt": compose_turn(
                group,
                agent,
                unseen,
                briefing=brief,
                also_addressed=turn.get("named") or [],
            ),
            "thread_id": group_thread_id(group, agent),
        }

    async def _record_reply(
        group_id: int,
        turn: dict,
        agent: str,
        text_out: str,
        *,
        segments: list[dict[str, Any]] | None = None,
    ) -> tuple[dict, str]:
        """Persist one member's reply and stamp who it hands the thread to."""
        store = get_group_store()
        handed_to = next_speaker(text_out, speaker=agent, members=turn["members"])
        agent_msg = await asyncio.to_thread(
            lambda: store.add_message(
                group_id,
                author=agent,
                author_kind="agent",
                body=text_out,
                addressee=handed_to,
                segments=segments,
            )
        )
        _publish(turn["group"], "message")
        return agent_msg, handed_to

    async def _note_member_failure(
        group_id: int, turn: dict, agent: str, detail: str
    ) -> dict | None:
        """Record, in the transcript, that one member of a fan-out never spoke.

        Same rule the cascade's stop notes follow: from the outside "beta had
        nothing to add" and "beta's turn crashed" are the same silence, and
        only one of them means the human should try again. Best-effort — a
        note that cannot be written must never cost the replies that did land.
        """
        text = str(detail or "").strip() or "unknown error"
        if len(text) > 300:
            text = text[:297].rstrip() + "…"
        try:
            note = await asyncio.to_thread(
                lambda: get_group_store().add_message(
                    group_id,
                    author="system",
                    author_kind="system",
                    body=f"@{agent} was asked but could not answer ({text}).",
                    addressee="",
                )
            )
        except Exception:  # pragma: no cover - a deleted group has nowhere to say it
            logger.debug("group %s: could not record %s's failure", group_id, agent, exc_info=True)
            return None
        _publish(turn["group"], "message")
        return note

    async def _cascade_after_fanout(
        group_id: int, turn: dict, replies: list[tuple[str, str, str]]
    ) -> dict:
        """Start the follow-on exchange this message's replies ask for, if any.

        ``replies`` is ``(speaker, text, handed_to)`` per member that answered,
        in the order they spoke. Two rules decide the seed:

        - **A hand-off to somebody who already spoke on this message is not a
          request.** The human addressed them both and they have both answered;
          handing it back would run one of them twice on one message.
        - **Otherwise the earliest un-served hand-off wins.** A group holds one
          ordered conversation, so at most one of several hand-offs can be
          served — and the earliest is both the one that has been waiting
          longest and the one that keeps the exchange reading in transcript
          order. The others are not lost to the reader: the ``@name`` is right
          there in the message that wrote it.

        Running this after the whole fan-out rather than per reply is the fix
        for a real hole: seeding only from the LAST speaker silently dropped an
        earlier member's "@maria can you take this?" whenever a later member
        settled — which is precisely the collaboration a group exists for.
        """
        already = set(turn.get("addressees") or [])
        for speaker, text_out, handed_to in replies:
            if handed_to and handed_to not in already:
                return await _maybe_cascade(
                    turn["settings"],
                    runtime,
                    group_id=group_id,
                    speaker=speaker,
                    reply=text_out,
                    handed_to=handed_to,
                )
        return {"running": False, "reason": "settled"}

    @app.post("/api/groups/{group_id}/messages")
    async def post_group_message(group_id: int, body: dict) -> dict:
        """Post a human message and let the members it named answer it.

        One turn each, in the order they were named, each one able to see what
        the ones before it said. ``addressee`` is whoever answered first (what
        the field has always meant); ``addressees`` is everyone that was asked.

        With ``groups.autonomous`` on, the LAST member's reply may hand the
        thread to somebody who has not yet spoken on this message: that
        exchange runs behind this response (see ``_maybe_cascade``), and the
        ``cascade`` key says whether one started.
        """
        turn = await _begin_group_turn(group_id, body)
        addressee, human_msg = turn["addressee"], turn["human_msg"]
        speakers: list[str] = turn["addressees"]
        if not speakers:
            # For the people, not the bots: saved, nobody woken, said so.
            return {
                "addressee": "",
                "addressees": [],
                "thread_id": "",
                "messages": [human_msg],
                "cascade": {"running": False, "reason": NOBODY_ADDRESSED},
            }
        replies: list[dict] = []
        handoffs: list[tuple[str, str, str]] = []
        thread_id = ""
        refusal = ""
        for speaker in speakers:
            member = await _member_turn(turn, speaker)
            # The FIRST speaker's thread, matching `addressee`: a fan-out runs
            # on one thread per member, so a single field can only honestly
            # name one of them.
            thread_id = thread_id or member["thread_id"]
            try:
                # This body still runs inside the middleware's context (identity
                # intact), but the middleware never sees an agent name — the
                # per-agent ownership answer has to be added per turn, exactly as
                # /api/chat does.
                with push_request_context(metadata={AGENT_ADMIN_KEY: member["agent_admin"]}):
                    reply = await member["runner"].ainvoke(
                        member["prompt"], thread_id=member["thread_id"]
                    )
            except Exception as exc:
                # A member that was asked and crashed leaves a system line, the
                # same discipline the cascade's stop notes follow: the group is
                # shared state, and a silence nobody can account for is worse
                # than a short explanation. The half-written reply itself is
                # never persisted — a transcript holds what was said.
                logger.exception("group turn failed for '%s'", speaker)
                refusal = refusal or str(exc)
                await _note_member_failure(group_id, turn, speaker, str(exc))
                continue
            if not str(reply).strip():
                # The store refuses empty bodies, so without this the GroupError
                # from _finish_group_turn escaped as an unhandled 500. An empty
                # answer is not a failure (the cascade settles on one too), so
                # it earns no system line — only the 502 below if nobody spoke.
                refusal = refusal or f"'{speaker}' produced no reply."
                continue
            # Attribution, both halves: who produced this message and who it
            # hands the conversation to. Stamped whether or not the cascade is
            # on — it describes the message, and the switch governs turn-taking,
            # not the record of it.
            agent_msg, handed_to = await _record_reply(group_id, turn, speaker, str(reply))
            replies.append(agent_msg)
            handoffs.append((speaker, str(reply), handed_to))
        if not replies:
            # Nobody answered at all. The human turn is already saved, so this
            # is a failed reply and not a failed message.
            raise HTTPException(status_code=502, detail=refusal or "No member answered.")
        cascade = await _cascade_after_fanout(group_id, turn, handoffs)
        return {
            "addressee": addressee,
            "addressees": speakers,
            "thread_id": thread_id,
            "messages": [human_msg, *replies],
            "cascade": cascade,
        }

    @app.post("/api/groups/{group_id}/messages/stream")
    async def post_group_message_stream(group_id: int, body: dict, request: Request) -> Any:
        """The same turn as the POST above, streamed as it is written.

        The blocking endpoint made a group turn the one conversation in the
        product where you watch a spinner instead of words arriving — the 1:1
        chat has streamed from day one. Frames are the same ``data: {json}``
        SSE shape as ``/api/chat/stream``::

            {"type": "start",        "addressee": str, "addressees": [str], "message": {...}}
            {"type": "speaker",      "author": str, "index": int, "of": int}
            {"type": "text",         "author": str, "text": str}
            {"type": "tool_call",    "author": str, "id": str, "name": str, "args": str}
            {"type": "tool_result",  "author": str, "id": str, "name": str, "content": str}
            {"type": "subagent_text","author": str, "namespace": [str], "text": str}
            {"type": "message",      "message": {...}, "handed_to": str}
            {"type": "cascade",      ...}
            {"type": "error",        "detail": str, "author": str}
            {"type": "done"}

        **Every frame after ``start`` names its author**, because a group turn
        can have more than one: a message that names several members gives each
        of them a turn, in the order they were named, and a ``speaker`` frame
        opens each one. Without the author on every frame the second member's
        words would land in the first member's bubble.

        **Tool and subagent frames are forwarded**, unlike the first version of
        this endpoint, which dropped everything but text. In a group the tools
        are half of what there is to watch — ``talk_to_agent`` reaching a peer,
        a subagent doing the actual research — and a transcript that shows only
        the final paragraph hides the collaboration the group exists for. The
        client decides how much of it to draw (quiet / normal / verbose); the
        stream's job is to carry it.

        Three deliberate departures from the chat stream:

        - **A disconnect does not abandon the turn.** The transcript is shared
          state: the human message is already persisted, and other people (and
          the cascade) may be watching the group. The turn runs as a detached
          task (:mod:`olano.core.runs`) and this response is only a *view* of
          it, so a browser leaving costs the viewer and never the work — and a
          viewer that comes back rejoins from its cursor via
          ``GET /api/groups/{id}/messages/attach``. Keeping the loop consuming
          after a disconnect, which is what this used to do, only held while
          the response task itself lived: Starlette cancels that task group on
          ``http.disconnect``, so the turn died anyway.
        - **An empty reply persists nothing.** The store refuses empty bodies;
          an ``error`` frame says so instead of a silent no-op.
        - **A failure part-way through a fan-out is not the end of the turn.**
          The members that already answered keep their replies, the failure is
          written into the transcript as a system line, and the stream carries
          on to the next member.

        Errors after the human turn is saved arrive as an ``error`` frame, not
        an HTTP status — the response is already 200 and streaming by then,
        which is the same contract the chat stream has.
        """
        turn = await _begin_group_turn(group_id, body)
        addressee = turn["addressee"]
        speakers: list[str] = turn["addressees"]

        async def _run_turn(run: AgentRun) -> None:
            # One workspace root per author, resolved once for the whole turn.
            roots: dict[str, Any] = {}

            def frame(payload: dict) -> None:
                run.emit(payload)

            frame(
                {
                    "type": "start",
                    "group_id": group_id,
                    "addressee": addressee,
                    "addressees": speakers,
                    "message": turn["human_msg"],
                }
            )
            handoffs: list[tuple[str, str, str]] = []
            for index, speaker in enumerate(speakers):
                member = await _member_turn(turn, speaker)
                frame(
                    {
                        "type": "speaker",
                        "author": speaker,
                        "index": index,
                        "of": len(speakers),
                    }
                )
                acc = SegmentAccumulator()
                try:
                    # This generator runs after Starlette's middleware has
                    # exited its own push_request_context — the same reason
                    # /api/chat/stream re-binds — so without this the turn runs
                    # on the unbound DEFAULT context, whose local-operator
                    # fallback the self_config ownership gate trusts. Re-bind
                    # the surface plus the ownership answer resolved in
                    # _begin_group_turn while the real session was still in hand.
                    with push_request_context(
                        source="api",
                        channel="api",
                        metadata={AGENT_ADMIN_KEY: member["agent_admin"]},
                    ):
                        async for ev in member["runner"].astream_events(
                            member["prompt"], thread_id=member["thread_id"]
                        ):
                            if not isinstance(ev, tuple) or len(ev) != 2:
                                continue
                            kind, payload = ev
                            out = _stream_frame(kind, payload, speaker)
                            if out is None:
                                continue
                            if kind == "text":
                                acc.text(str(out.get("text") or ""))
                            elif kind == "tool_call":
                                acc.tool_call(out.get("id"))
                            elif kind == "tool_result" and SCAN_TOOL_RESULTS:
                                # Off — see `media_refs.SCAN_TOOL_RESULTS`. Files
                                # a tool result merely NAMED are no longer
                                # attached: intermediate and irrelevant files (a
                                # read file's own contents cite paths too) were
                                # landing on the member's bubble. The speaker's
                                # reply below is the hand-off, and it already
                                # resolves against that member's own workspace.
                                # Read from the flag rather than deleted so this
                                # surface and the 1:1 chat cannot disagree.
                                refs = _member_media(str(out.get("content") or ""), speaker, roots)
                                if refs:
                                    out["media"] = refs
                            frame(out)
                except Exception as exc:
                    logger.exception("group stream turn failed for '%s'", speaker)
                    frame({"type": "error", "detail": str(exc), "author": speaker})
                    await _note_member_failure(group_id, turn, speaker, str(exc))
                    continue
                # `.strip()` is safe for the segments invariant: `segmentsCover`
                # compares whitespace-stripped joins on both sides. Anything that
                # rewrote the body more than that would have to rewrite the spans
                # with it, or interleaving silently turns itself off.
                text_out = acc.body.strip()
                if not text_out:
                    frame(
                        {
                            "type": "error",
                            "detail": f"'{speaker}' produced no reply.",
                            "author": speaker,
                        }
                    )
                    continue
                reply_media = _member_media(text_out, speaker, roots)
                agent_msg, handed_to = await _record_reply(
                    group_id, turn, speaker, text_out, segments=acc.segments()
                )
                handoffs.append((speaker, text_out, handed_to))
                if reply_media:
                    # Before `message`, which settles the live turn: this is
                    # what puts the file on the bubble the client is still
                    # streaming into. The row carries its own copy for the
                    # reload, and the client dedupes by url.
                    frame({"type": "media", "author": speaker, "items": reply_media})
                frame({"type": "message", "message": agent_msg, "handed_to": handed_to})
            # One `cascade` frame per turn, after the whole fan-out: a group
            # holds one ordered conversation, so the hand-offs compete for it.
            if speakers:
                cascade = await _cascade_after_fanout(group_id, turn, handoffs)
            else:
                cascade = {"running": False, "reason": NOBODY_ADDRESSED}
            frame({"type": "cascade", **cascade})
            frame({"type": "done"})

        run = _RUNS.begin("group", _GROUP_RUN_AGENT, str(group_id), turn.get("turn_id") or "")
        if run is None:
            live = _RUNS.for_thread("group", _GROUP_RUN_AGENT, str(group_id))
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "run_in_progress",
                    "message": "This group is already working on a turn.",
                    "run_id": getattr(live, "run_id", ""),
                },
            )

        async def _drive() -> None:
            try:
                await _run_turn(run)
            except asyncio.CancelledError:
                run.emit({"type": "error", "detail": "Stopped.", "author": addressee})
                run.emit({"type": "done"})
                raise
            except Exception as exc:  # pragma: no cover - defensive
                logger.exception("group %s: detached turn failed", group_id)
                run.emit({"type": "error", "detail": str(exc), "author": addressee})
                # Terminal either way: a viewer must be able to tell "this turn
                # ended badly" from "my connection dropped", which is the whole
                # distinction detached runs exist to restore.
                run.emit({"type": "done"})
            finally:
                _RUNS.finish(run)
                _publish(group_id, "run_ended", run_id=run.run_id)

        run.task = asyncio.get_running_loop().create_task(
            _drive(), name=f"group-run:{group_id}:{run.run_id}"
        )
        _publish(group_id, "run_started", run_id=run.run_id)

        from starlette.responses import StreamingResponse

        return StreamingResponse(_sse_run_view(run, 0, request), media_type="text/event-stream")

    @app.get("/api/groups/{group_id}/messages/attach")
    async def attach_group_stream(group_id: int, request: Request, cursor: int = 0) -> Any:
        """Rejoin a group turn already running on this box.

        Same recovery as ``GET /api/chat/attach``, and for the same reason: the
        turn outlives the socket, so a browser that dropped out replays what it
        missed from ``cursor`` and then tails. A group has the extra motive that
        several people may be watching one transcript — a reconnect must not
        cost any of them the middle of the exchange.
        """
        from starlette.responses import StreamingResponse

        _require_enabled()
        user = _current_user()
        await asyncio.to_thread(lambda: _require_visible(group_id, user))
        run = _RUNS.latest_for_thread("group", _GROUP_RUN_AGENT, str(group_id))
        if run is None:
            raise HTTPException(status_code=404, detail="no such run")
        return StreamingResponse(
            _sse_run_view(run, max(0, int(cursor)), request), media_type="text/event-stream"
        )

    @app.get("/api/groups/{group_id}/cascade")
    async def group_cascade_status(group_id: int) -> dict:
        """Is a bot-to-bot exchange running in this group, and on what budget.

        The transcript itself rides the existing ``/api/stream`` bus (see
        ``_publish``); this is the small piece of state that is *not* in the
        transcript — who is mid-turn, how many hops are left — which is what a
        Stop button needs to render.
        """
        settings = _require_enabled()
        user = _current_user()
        await asyncio.to_thread(lambda: _require_visible(group_id, user))
        state = REGISTRY.get(group_id)
        return {
            "cascade": state.snapshot() if state is not None else {"running": False},
            "settings": {
                "autonomous": settings.autonomous,
                "max_hops": settings.max_hops,
                "cascade_timeout_seconds": settings.cascade_timeout_seconds,
            },
        }

    @app.post("/api/groups/{group_id}/stop")
    async def stop_group_cascade(group_id: int) -> dict:
        """Cut off a running bot-to-bot exchange.

        Sets the stop flag *and* cancels the in-flight hop: a human watching
        two agents talk must not have to wait out a model call that may take
        minutes. Whatever was already said stays in the transcript, and the
        cascade records why it ended.
        """
        _require_enabled()
        user = _current_user()
        await asyncio.to_thread(lambda: _require_visible(group_id, user))
        stopped = REGISTRY.stop(group_id)
        if stopped:
            _publish(group_id, "cascade_stopping")
        return {"ok": True, "stopped": stopped}


def _publish(group: dict | int, kind: str, **extra: Any) -> None:
    """Poke every dashboard on the existing ``/api/stream`` bus.

    Deliberately **content-free** — an id, a kind, and cascade counters. A
    second per-group SSE endpoint would duplicate a bus that already exists,
    and putting message bodies (or member names) on the shared one would hand
    them to every subscriber: ``auth.event_visible_to_agents`` only scopes
    frames it can attribute to an agent, which is exactly why the frames that
    pass unfiltered are the ones that "carry no content and only trigger
    refetches of endpoints that filter server-side". So the cascade pokes once
    per message as it lands, and the client re-reads
    ``GET /api/groups/{id}/messages``, which applies the same visibility rules
    as everything else.
    """
    group_id = group if isinstance(group, int) else group.get("id")
    try:
        from olano.runtime.api import _push_event

        _push_event("group_updated", {"id": group_id, "kind": kind, **extra})
    except Exception:  # pragma: no cover - SSE is best-effort
        pass
