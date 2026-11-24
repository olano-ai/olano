"""Autonomous bot-to-bot cascades inside a group thread.

A human posts once; the addressed member answers (``groups_routes``); and if
that answer hands the conversation to *another* member — by ``@name`` — this
module gives that member the next turn, and so on, with no human in the loop.
That is the whole feature, and it is the dangerous half of the group surface:
**an agent loop that can start another agent loop**. Everything here exists to
bound it.

Five bounds, none of them optional, all of them visible in the transcript:

* **Hops** — ``groups.max_hops`` (default 3) turns per human message. When the
  budget runs out the thread settles with a ``system`` message saying so; a
  cascade never dies quietly, because "the bots stopped talking" and "the bots
  hit their limit" look identical from the outside.
* **Wall clock** — ``groups.cascade_timeout_seconds`` (default 180) covers the
  *whole* cascade, and each hop is additionally capped at whatever is left of
  it, so one slow model cannot pin a worker past the budget.
* **Ping-pong** — the same ordered pair (``alpha``→``beta``) may fire once per
  cascade. Two agents that keep tossing the conversation back and forth would
  otherwise burn the entire hop budget saying "over to you".
* **Roster + access, re-checked every hop** — a mention only counts if it names
  a current member *and* the originating human may still reach that agent. The
  roster is not a permission (the human path makes the same check on every
  turn), and a grant revoked mid-cascade takes effect at the very next hop.
* **Self-address is never a hop** — an agent that writes its own ``@name``
  is talking about itself, not scheduling itself.

Every message is written to the store **as it is produced**, never buffered:
a cascade that dies at hop 3 leaves hops 1–2 in the transcript, which is the
only behaviour that makes a partial exchange readable rather than lost.

Nothing here is a second agent loop: a hop is an ordinary
:class:`~olano.core.runner.AgentRunner` turn on the same group-scoped thread id
the human path uses, so checkpointing, credentials, stream isolation and cost
accounting behave exactly as they do in a 1:1 chat.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from olano.security.request_context import AGENT_ADMIN_KEY, push_request_context

logger = logging.getLogger(__name__)

#: ``@name`` mentions. Shared with ``groups_routes.pick_addressee`` (and
#: mirrored client-side in ``groupsStore.mentionedMembers``) so the human path
#: and the bot path can never disagree about what a mention is. The last
#: captured character must be a word character: ``.`` and ``-`` may appear
#: *inside* a handle, but a mention that ends a sentence — "over to you,
#: @beta." — names ``beta``, not ``beta.``, which used to match no member and
#: silently settle the thread.
MENTION_RE = re.compile(r"@([A-Za-z0-9_.-]*[A-Za-z0-9_])")

#: How much of the transcript one turn may carry into an agent's prompt. Each
#: member holds everything up to its own last message in its checkpoint, so
#: this only bounds the catch-up block.
MAX_CONTEXT_MESSAGES = 30

# ── stop reasons ─────────────────────────────────────────────────────────────
# The cascade always ends for exactly one of these, and every reason except
# SETTLED/GONE writes itself into the transcript.
SETTLED = "settled"  # nobody was addressed — the conversation finished on its own
HOP_CAP = "hop_cap"  # groups.max_hops spent
TIMEOUT = "timeout"  # groups.cascade_timeout_seconds spent
PING_PONG = "ping_pong"  # the same ordered pair came round again
STOPPED = "stopped"  # a human cut it off
ERROR = "error"  # a turn raised
ACCESS_DENIED = "access_denied"  # the originating human may no longer reach that agent
OFFLINE = "offline"  # the addressed member has no running runner
GONE = "gone"  # the group was deleted mid-cascade


@dataclass(frozen=True)
class CascadeLimits:
    """The two budgets, resolved from the ``groups:`` config block."""

    max_hops: int = 3
    timeout_seconds: float = 180.0

    @classmethod
    def from_settings(cls, settings: Any) -> CascadeLimits:
        return cls(
            max_hops=int(getattr(settings, "max_hops", 3) or 0),
            timeout_seconds=float(getattr(settings, "cascade_timeout_seconds", 180.0) or 0.0),
        )


@dataclass
class CascadeResult:
    """What a finished cascade did — the reason is what the transcript says."""

    hops: int = 0
    reason: str = SETTLED
    note: str = ""
    messages: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"hops": self.hops, "reason": self.reason, "note": self.note}


@dataclass
class CascadeState:
    """Live state of one running cascade — what ``/stop`` cancels and what the
    status endpoint reports. One per group: a group holds a single ordered
    conversation, so two cascades in it would interleave two exchanges into one
    transcript."""

    group_id: int
    max_hops: int = 0
    hops: int = 0
    speaker: str = ""  # the member currently taking a turn ("" between hops)
    started_at: float = 0.0
    deadline: float = 0.0
    stopped: bool = False
    # An asyncio.Task on the loop paths, a concurrent.futures.Future when a
    # worker-thread tool scheduled the cascade via run_coroutine_threadsafe.
    # Both answer cancel() and done(), which is all anything asks of it.
    task: Any | None = None

    def snapshot(self, *, clock: Callable[[], float] = time.monotonic) -> dict[str, Any]:
        return {
            "group_id": self.group_id,
            "running": True,
            "hops": self.hops,
            "max_hops": self.max_hops,
            "speaker": self.speaker,
            "stopping": self.stopped,
            "seconds_left": max(0.0, round(self.deadline - clock(), 1)),
        }


class CascadeRegistry:
    """The running cascades, keyed by group id.

    Deliberately in-process: a cascade *is* a set of in-flight coroutines on
    this runtime's event loop, so anything that outlived the process would
    describe work nobody can still stop.
    """

    def __init__(self) -> None:
        self._by_group: dict[int, CascadeState] = {}
        self._lock = threading.Lock()

    def begin(self, group_id: int, *, limits: CascadeLimits) -> CascadeState | None:
        """Claim the group. ``None`` when a cascade is already running in it —
        the caller answers the human once and simply does not cascade, rather
        than braiding two exchanges into one transcript."""
        with self._lock:
            live = self._by_group.get(group_id)
            if live is not None and not self._finished(live):
                return None
            now = time.monotonic()
            state = CascadeState(
                group_id=group_id,
                max_hops=limits.max_hops,
                started_at=now,
                deadline=now + limits.timeout_seconds,
            )
            self._by_group[group_id] = state
            return state

    def finish(self, state: CascadeState) -> None:
        with self._lock:
            if self._by_group.get(state.group_id) is state:
                self._by_group.pop(state.group_id, None)

    def get(self, group_id: int) -> CascadeState | None:
        with self._lock:
            state = self._by_group.get(group_id)
            if state is not None and self._finished(state):
                self._by_group.pop(group_id, None)
                return None
            return state

    def stop(self, group_id: int) -> bool:
        """Cut a cascade off. Returns whether one was running.

        Two mechanisms, both needed: the flag stops the loop between hops even
        when the task cannot be cancelled, and the cancel interrupts a hop that
        is already inside a model call — a human who presses Stop must not have
        to wait out a turn that may take minutes.
        """
        state = self.get(group_id)
        if state is None:
            return False
        state.stopped = True
        task = state.task
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:  # pragma: no cover - defensive
                logger.debug("group %s: cascade cancel failed", group_id, exc_info=True)
        return True

    def reset(self) -> None:
        """Drop every entry (tests; a runtime restart)."""
        with self._lock:
            self._by_group.clear()

    @staticmethod
    def _finished(state: CascadeState) -> bool:
        task = state.task
        return task is not None and task.done()


#: Process-wide registry — the runtime is one process and a cascade is bound to
#: its event loop.
REGISTRY = CascadeRegistry()


# ── pure helpers ─────────────────────────────────────────────────────────────


def mentioned_members(text: str, members: list[str]) -> list[str]:
    """Members named by ``@name`` in ``text``, in order, de-duplicated.

    Case-insensitive so a model writing ``@Beta`` reaches ``beta``; a mention
    that names no member is dropped here rather than anywhere downstream —
    a bot may only ever address someone who is in the room.
    """
    by_lower = {m.lower(): m for m in members}
    out: list[str] = []
    for raw in MENTION_RE.findall(text or ""):
        member = by_lower.get(raw.lower())
        if member and member not in out:
            out.append(member)
    return out


def next_speaker(text: str, *, speaker: str, members: list[str]) -> str:
    """Who this reply hands the conversation to — ``""`` for nobody.

    The first mentioned member that is not the speaker itself. Self-address is
    never a hop: an agent writing its own name is referring to itself, and
    scheduling it would be a one-agent infinite loop that no budget short of
    the hop cap could distinguish from progress.
    """
    for member in mentioned_members(text, members):
        if member.lower() != (speaker or "").lower():
            return member
    return ""


def render_transcript(unseen: list[dict]) -> str:
    """The catch-up block for a turn's prompt (oldest→newest, bounded).

    A cascade hop carries ``reply_to`` — the member that called it — and the
    block says so (``beta (to alpha): …``), so the next reader can tell an
    answer from a new thought without the answering bot having to write the
    caller's ``@handle`` into its text, which would read as a hand-off.
    """
    lines: list[str] = []
    for m in unseen[-MAX_CONTEXT_MESSAGES:]:
        reply_to = str(m.get("reply_to") or "").strip()
        who = f"{m['author']} (to {reply_to})" if reply_to else m["author"]
        lines.append(f"{who}: {m['body']}")
    return "\n".join(lines) if lines else "(no new messages)"


def roster_block(group: dict, agent: str, roster: list[dict] | None) -> str:
    """The room, one line per member: handle, whether it is you, what it does.

    A group is only useful if a member can tell *who to pull in for what*, and
    a bare list of names carries none of that — "the other members are: curtis,
    maria" tells a model nothing about which of them owns pricing. So each line
    carries the member's own description, which is the same sentence a human
    reads beside it in the sidebar.

    ``roster`` is optional and per-entry lenient: a member with nothing on file
    still gets a line, because a missing description must never drop somebody
    out of the room.
    """
    by_name = {str(r.get("name") or ""): r for r in (roster or [])}
    lines: list[str] = []
    for member in group.get("members") or []:
        info = by_name.get(member) or {}
        desc = str(info.get("description") or "").strip().replace("\n", " ")
        if len(desc) > 220:
            desc = desc[:217].rstrip() + "…"
        mine = " (this is you)" if member == agent else ""
        offline = "" if info.get("online", True) else " [not running right now]"
        lines.append(f"- @{member}{mine} — {desc or 'no description on file'}{offline}")
    return "\n".join(lines) or "- (nobody else)"


def people_block(humans: list[dict] | None) -> str:
    """The people in the room, one line each: handle, name, how they got here.

    A human turn in the transcript is labelled ``human-<handle>``, and with two
    people posting the model has to be able to tell them apart and answer the
    one who asked. So each line carries the handle exactly as the transcript
    spells it, the display name beside it, and whether that person made the
    group, was let into it, or is an admin who simply posted, the same three
    kinds the dashboard's People section shows.

    ``humans`` is optional and per-entry lenient, like :func:`roster_block`:
    an entry with only a handle still gets a line.
    """
    lines: list[str] = []
    for person in humans or []:
        handle = str(person.get("username") or "").strip()
        principal = str(person.get("principal") or "").strip() or (
            f"human-{handle}" if handle else ""
        )
        if not principal:
            continue
        name = str(person.get("name") or "").strip().replace("\n", " ")
        role = str(person.get("role") or "").strip()
        how = {
            "creator": "created this group",
            "shared": "was let in by the creator",
            "spoke": "an admin who posted here",
        }.get(role, "")
        label = f"- {principal}"
        if name and name.lower() != handle.lower():
            label += f" ({name})"
        if how:
            label += f", {how}"
        lines.append(label)
    return "\n".join(lines) or "- (one person, the one you are talking to)"


def tools_block(peer_tools: list[str] | None, subagents: list[str] | None) -> str:
    """The ways this member can reach another agent, naming only what it holds.

    Listing a tool an agent was never bound to is worse than listing nothing:
    the model plans around it and then fails the call. So every line here is
    gated on the caller having actually resolved that tool for this agent.
    """
    held = set(peer_tools or [])
    lines: list[str] = [
        "- **@mention in your reply** — the visible route. It happens in this "
        "transcript, where the human can read it, and it is the right default.",
    ]
    if "talk_to_agent" in held:
        lines.append(
            "- **talk_to_agent(agent_name, message)** — a private side channel to "
            "another bot on this box. Its answer comes back to you inside this "
            "same turn, so use it when you need a fact in order to reply; use an "
            "@mention when the exchange itself is what the human wants to see."
        )
    if subagents:
        listed = ", ".join(subagents[:8])
        lines.append(
            f"- **task(...)** — delegate to your own subagents ({listed}). They "
            "work for you and are invisible to the group, so their findings are "
            "yours to report."
        )
    if "group_read" in held or "group_list" in held:
        lines.append(
            "- **group_read / group_list** — re-read this or another of your "
            "group chats if you need more than the catch-up below."
        )
    if "group_send" in held:
        lines.append(
            "- **group_send(group, text, to)** — post into one of your groups "
            "outside a turn. You do NOT need it to answer here: just reply."
        )
    return "\n".join(lines)


def group_briefing(
    group: dict,
    agent: str,
    *,
    roster: list[dict] | None = None,
    peer_tools: list[str] | None = None,
    subagents: list[str] | None = None,
    autonomous: bool = False,
    humans: list[dict] | None = None,
) -> str:
    """The standing rules of a group chat, from one member's point of view.

    Every turn in a group — the human path and every cascade hop — prepends
    this, so the members cannot disagree about what ``@name`` means or about
    who is in the room. It is pure text assembly on purpose: no runtime, no
    auth, no store, so the wording is testable and the callers stay the ones
    that know which tools an agent actually holds.

    The rule it exists to state is the one a model gets wrong on its own: a
    message that names somebody else is **context, not silence** — the member
    reads it, learns from it, and stays out of the way unless it was named too
    or holds something the answer genuinely needs.

    ``humans`` lists the people in the room (see :func:`people_block`). With
    more than one of them the room has a second rule the model cannot infer:
    a message from a person that names no bot is **for the other people**, and
    nobody answers it, which is why the block says so only then, and why a
    one-person room keeps reading as "the human".
    """
    if autonomous:
        handoff = (
            "Whoever you name is handed the next turn automatically, so name "
            "somebody only when you actually need them — an @mention you did "
            "not mean costs a real model call."
        )
    else:
        handoff = (
            "Follow-on turns are off on this deployment, so an @mention marks "
            "who should pick this up rather than waking them — say plainly what "
            "you need from them so the human can pass it on."
        )
    several_people = len(humans or []) > 1
    if several_people:
        people_rule = (
            "- Several PEOPLE are in this room, and each of their messages is "
            "labelled with their handle (`human-<handle>`). Answer the person "
            "who asked, by name when it matters, and remember the others are "
            "reading too. A message from a person that names no bot is for the "
            "other people, not for you, nobody answers it.\n"
        )
    else:
        people_rule = (
            "- The person's messages are labelled with their handle "
            "(`human-<handle>`); you are talking to that one person.\n"
        )
    return (
        f'You are "{agent}", one of the bots in the group chat '
        f'"{group.get("name", "")}".\n\n'
        f"WHO IS IN THIS ROOM\n{roster_block(group, agent, roster)}\n\n"
        f"PEOPLE IN THIS ROOM\n{people_block(humans)}\n\n"
        "HOW THIS CHAT WORKS\n"
        "- Everyone here sees every message. The catch-up block below carries "
        "everything said since your last turn, including what the other bots "
        "said to each other — read it before you answer.\n"
        f"{people_rule}"
        "- `@name` is how a message is addressed. A message naming YOU is yours "
        "to answer. A message naming somebody else is context: read it, do not "
        "answer for them, and stay out of it unless you were named too or you "
        "hold something the answer genuinely needs — in which case add it "
        "briefly and say why it is yours.\n"
        "- One message can name several of us. We each take our own turn, in "
        "the order we were named, and each of us can see what the ones before "
        "said — so do YOUR part of it and do not repeat theirs.\n"
        f"- To bring a teammate in, write `@their-name` and say what you need. "
        f"{handoff}\n"
        "- Never answer in another member's voice, never invent what they said, "
        "and never write your own @name — it does nothing.\n\n"
        f"HOW TO REACH THE OTHERS\n{tools_block(peer_tools, subagents)}\n\n"
        "Reply in your own voice and keep it to what a person would actually "
        "read in a chat."
    )


def compose_hop_turn(
    group: dict,
    agent: str,
    unseen: list[dict],
    *,
    called_by: str,
    hops_left: int,
    briefing: str = "",
) -> str:
    """The prompt for a bot-to-bot hop.

    Framed differently from the human path on purpose: the agent is told that
    a *peer* addressed it, that no human is waiting on this turn, and how much
    budget is left — a model that knows the exchange is nearly over wraps up
    instead of handing off again one hop before the cap cuts it.

    ``briefing`` is the shared :func:`group_briefing`, injected by the caller
    (which is the layer that knows the roster and the member's tools). Empty
    falls back to the one-line roster this prompt has always carried, so a
    cascade driven from a context with no runtime still reads correctly.
    """
    if not briefing:
        others = [m for m in group.get("members", []) if m != agent] or ["nobody yet"]
        briefing = (
            f'You are "{agent}" in the group chat "{group.get("name", "")}". '
            f"The other members are: {', '.join(others)}."
        )
    if hops_left <= 0:
        budget = (
            "This is the last turn in this exchange: nobody can be handed the "
            "conversation after you, so finish the thought yourself."
        )
    else:
        budget = (
            f"At most {hops_left} more bot-to-bot turn(s) can follow yours "
            "before the thread waits for a human."
        )
    return (
        f"{briefing}\n\n"
        f"{called_by} addressed you directly — this is an exchange between the "
        "agents in this group, with no human waiting mid-thread. Reply in your "
        "own voice, do the part that is actually yours, and keep it short. "
        "Mention a member by @name ONLY if you genuinely need them to take the "
        "next turn; if the work is done or the answer is yours to give, say so "
        f"and mention nobody. {budget}\n\n"
        f"New messages since your last turn:\n{render_transcript(unseen)}"
    )


def stop_note(reason: str, *, limits: CascadeLimits, detail: str = "") -> str:
    """The ``system`` line a stopped cascade leaves in the transcript.

    Every budget that fires is *visible*: from the outside "the agents finished"
    and "the agents were cut off" are the same silence, and only one of them
    means the human should say something.
    """
    who = f"'{detail}'" if detail else "the next agent"
    if reason == HOP_CAP:
        return (
            f"Agents stopped after {limits.max_hops} hop(s) — the limit set by "
            f"groups.max_hops. {who} did not get a turn. Reply to continue the thread."
        )
    if reason == TIMEOUT:
        return (
            f"Agents stopped after {int(limits.timeout_seconds)}s — the limit set by "
            "groups.cascade_timeout_seconds. Everything said so far is kept above."
        )
    if reason == PING_PONG:
        pair = detail or "two members"
        return (
            f"Agents stopped: {pair} were handing the same exchange back and forth. "
            "Reply to steer them."
        )
    if reason == STOPPED:
        return "Agents stopped: a human ended the exchange."
    if reason == ACCESS_DENIED:
        return f"Agents stopped: you no longer have access to {who}."
    if reason == OFFLINE:
        return f"Agents stopped: {who} is not running."
    if reason == ERROR:
        return f"Agents stopped: a turn failed ({detail or 'unknown error'})."
    return ""


# ── the driver ───────────────────────────────────────────────────────────────


async def run_cascade(
    *,
    group_id: int,
    speaker: str,
    reply: str,
    store: Any,
    limits: CascadeLimits,
    runner_for: Callable[[str], Any],
    assert_access: Callable[[str], Any],
    thread_id_for: Callable[[str], str],
    publish: Callable[..., None] | None = None,
    briefing_for: Callable[[dict, str], str] | None = None,
    state: CascadeState | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CascadeResult:
    """Run bot-to-bot turns until the exchange settles or a budget is spent.

    ``speaker``/``reply`` are the turn that *started* the cascade (usually the
    member that answered the human). Every collaborator is injected — the
    runner lookup, the per-hop access check, the thread-id namer, the SSE poke,
    the per-member briefing — so the loop itself carries no runtime, no auth and
    no FastAPI, and its failure modes are testable without any of them.

    ``briefing_for(group, agent)`` supplies the standing rules of the room for
    the member about to speak (see :func:`group_briefing`). It is injected for
    the same reason the runner lookup is: the roster's descriptions and the
    member's own tool list live in the runtime, and this module must not reach
    for them. Omitted, every hop falls back to the bare roster line.
    """
    state = state or CascadeState(group_id=group_id, max_hops=limits.max_hops)
    deadline = clock() + max(0.0, float(limits.timeout_seconds))
    state.deadline = deadline
    state.max_hops = limits.max_hops

    result = CascadeResult()
    pairs_seen: set[tuple[str, str]] = set()
    current_speaker, current_text = speaker, reply
    detail = ""
    reason = SETTLED
    cancelled_externally = False

    try:
        while True:
            # The roster is re-read every hop: a member removed mid-cascade must
            # not get one more turn, and a deleted group must not be written to.
            group = store.get(group_id)
            if group is None:
                reason = GONE
                break
            members: list[str] = list(group.get("members") or [])

            nxt = next_speaker(current_text, speaker=current_speaker, members=members)
            if not nxt:
                reason = SETTLED
                break
            if state.stopped:
                reason = STOPPED
                break
            if result.hops >= limits.max_hops:
                reason, detail = HOP_CAP, nxt
                break
            pair = (current_speaker, nxt)
            if pair in pairs_seen:
                reason, detail = PING_PONG, f"{current_speaker} and {nxt}"
                break
            remaining = deadline - clock()
            if remaining <= 0:
                reason, detail = TIMEOUT, nxt
                break
            try:
                # Exactly the check the human path makes on every turn: the
                # roster is not a standing permission, and a grant revoked
                # after the group was formed must bite at the next hop.
                assert_access(nxt)
            except Exception as exc:
                logger.info("group %s: cascade blocked on '%s': %s", group_id, nxt, exc)
                reason, detail = ACCESS_DENIED, nxt
                break
            runner = runner_for(nxt)
            if runner is None:
                reason, detail = OFFLINE, nxt
                break

            pairs_seen.add(pair)
            result.hops += 1
            state.hops = result.hops
            state.speaker = nxt
            try:
                unseen = await asyncio.to_thread(store.unseen_for, group_id, nxt)
                brief = ""
                if briefing_for is not None:
                    try:
                        brief = briefing_for(group, nxt)
                    except Exception:  # pragma: no cover - a briefing is never load-bearing
                        logger.debug(
                            "group %s: briefing failed for %s", group_id, nxt, exc_info=True
                        )
                prompt = compose_hop_turn(
                    group,
                    nxt,
                    unseen,
                    called_by=current_speaker,
                    hops_left=limits.max_hops - result.hops,
                    briefing=brief,
                )
                # Each hop is capped at whatever is left of the whole-cascade
                # budget, so the wall clock holds even if a single model call
                # never returns.
                #
                # A hop is one agent answering another — no human owner is on
                # the turn, so the self_config ownership gate must refuse. The
                # cascade task's snapshot may have inherited a dashboard
                # turn's AGENT_ADMIN_KEY (context metadata merges), so bind
                # the hop's own answer rather than trusting the inheritance.
                with push_request_context(metadata={AGENT_ADMIN_KEY: False}):
                    text = await asyncio.wait_for(
                        runner.ainvoke(prompt, thread_id=thread_id_for(nxt)),
                        timeout=remaining,
                    )
            except TimeoutError:
                reason, detail = TIMEOUT, nxt
                break
            except asyncio.CancelledError:
                # Either the human pressed Stop (registry.stop cancels the task)
                # or the loop is going down. Both settle the thread; only the
                # second one is re-raised, below.
                cancelled_externally = not state.stopped
                reason = STOPPED
                break
            except Exception as exc:
                logger.exception("group %s: cascade hop for '%s' failed", group_id, nxt)
                reason, detail = ERROR, f"{nxt}: {exc}"
                break
            finally:
                state.speaker = ""

            body = str(text or "").strip()
            if not body:
                # An empty answer hands the conversation to nobody.
                reason = SETTLED
                break
            addressee = next_speaker(body, speaker=nxt, members=members)
            try:
                # Two attributions, kept apart on purpose: ``addressee`` is who
                # this reply hands the thread to (routing), ``reply_to`` is who
                # it is answering (structure). Folding the second into the text
                # as ``@caller`` would make every answer look like a hand-back.
                message = await asyncio.to_thread(
                    store.add_message,
                    group_id,
                    author=nxt,
                    author_kind="agent",
                    body=body,
                    addressee=addressee,
                    reply_to=current_speaker,
                )
            except Exception:
                logger.exception("group %s: could not persist hop %s", group_id, result.hops)
                reason = GONE
                break
            result.messages.append(message)
            if publish is not None:
                publish("message", hop=result.hops)
            current_speaker, current_text = nxt, body
    finally:
        state.speaker = ""

    result.reason = reason
    result.note = stop_note(reason, limits=limits, detail=detail)
    if result.note:
        # Written synchronously, not through ``asyncio.to_thread``: this runs on
        # the STOPPED path too, where the task is already carrying a
        # cancellation and the next ``await`` may never resume. The note is the
        # user-visible half of every budget, so it must not be the thing that
        # gets dropped. It is one local sqlite insert.
        try:
            result.messages.append(
                store.add_message(
                    group_id,
                    author="system",
                    author_kind="system",
                    body=result.note,
                    addressee="",
                )
            )
        except Exception:  # pragma: no cover - a deleted group has nowhere to say it
            logger.debug("group %s: could not record the stop note", group_id, exc_info=True)
    if publish is not None:
        publish("cascade_end", hop=result.hops, reason=reason)
    if cancelled_externally:
        raise asyncio.CancelledError
    return result
