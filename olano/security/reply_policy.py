"""Inbound size caps, silence rules, and the silence marker — one definition.

``agent.json5 → message_policy`` (:class:`~olano.config.agent_config.MessagePolicyConfig`)
answers two questions this module implements: *may this message reach the
model*, and *did the model decide to say nothing*. Everything here is pure — no
runtime, no I/O — because five surfaces ask it and they must not be able to
disagree:

* ``runtime/server.py`` — platform adapters (Telegram, WhatsApp, Discord, …)
* ``runtime/server.py`` — the classic channel bus
* ``runtime/api.py`` — dashboard + REST chat
* ``platforms/api_server.py`` — the OpenAI-compatible server
* ``core/runner.py`` — the stream filter that keeps the marker off the wire

**Ingress only, deliberately.** Scheduled, heartbeat and cognition turns reach
the runner directly and never cross these seams, so a capped support desk can
still write a long nightly report. That asymmetry is the feature, not a gap.

Two halves, priced very differently, and the config's help text says so:

``ignore_patterns`` are regexes matched here, before any model call — free,
exact, and nothing a sender writes can argue with them. ``ignore_rules`` are
sentences the model judges, which is the only way to answer "is this English"
or "is this gibberish"; the cost is a real turn, bounded by ``max_input_chars``
on the way in and ``max_output_tokens`` on the way out.

The marker
----------
A rule-based decision has to travel from the model back to the runtime, and the
only channel a model reliably has is its own text. So the prompt block tells it
to reply with exactly :data:`SILENCE_MARKER` and nothing else; the runtime
recognises that and sends nothing.

Two properties make it safe. It is matched **only as the reply's opening**
(:func:`is_silenced`), so an agent discussing the marker mid-sentence — or a
sender pasting it — cannot silence a turn that was going to answer. And
:class:`SilenceFilter` holds back at most ``len(SILENCE_MARKER)`` characters of
the stream while it decides, so the marker never reaches a dashboard, a chat
bubble or a transcript: once the opening cannot be the marker the filter flushes
and is a pass-through for the rest of the turn, allocating nothing.

A sender cannot forge the decision either way. The marker is judged on the
*model's* output, never on inbound text, and inbound text from an untrusted
sender is already wrapped by ``security/untrusted_ingress``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:  # pragma: no cover - typing only
    from olano.config.agent_config import AgentConfig, MessagePolicyConfig

logger = logging.getLogger(__name__)

#: What the model writes instead of a reply when an ``ignore_rules`` condition
#: holds. Deliberately bracket-wrapped and lowercase-underscored so it cannot
#: collide with ordinary prose, and short enough that
#: :class:`SilenceFilter` buffers an imperceptible amount of the stream.
SILENCE_MARKER = "[[no_reply]]"

#: Patterns are matched unanchored and case-insensitively — an operator writing
#: `crypto` means "mentions crypto", and demanding `(?i)` on every line is a
#: trap that fails silently. `^…$` is still available for whole-message rules.
_PATTERN_FLAGS = re.IGNORECASE

#: Compiled-pattern memo, keyed by the pattern list. Ingress runs per message;
#: recompiling a support desk's ten patterns per inbound is pure waste.
_COMPILED: dict[tuple[str, ...], tuple[re.Pattern[str], ...]] = {}

#: Patterns already reported as uncompilable, so a config that predates the
#: config-time validator logs once rather than per message.
_WARNED_BAD: set[str] = set()

Action = Literal["allow", "truncate", "reject", "ignore"]


@dataclass(frozen=True)
class InboundVerdict:
    """What ingress should do with one inbound message.

    ``action``
        ``allow`` — hand ``text`` to the agent unchanged (the common case).
        ``truncate`` — hand the shortened ``text`` to the agent.
        ``reject`` — do not run a turn; reply with ``notice``.
        ``ignore`` — do not run a turn; reply with ``notice`` only if it is
        non-empty, otherwise send nothing at all.

    ``notice`` is always a complete user-facing string (never a template), so a
    caller only has to decide *whether* to send it. ``reason`` is for logs and
    the ``message_policy`` event — never shown to a sender, because telling a
    spammer which pattern caught them is a tuning guide.
    """

    action: Action = "allow"
    text: str = ""
    notice: str = ""
    reason: str = ""

    @property
    def blocked(self) -> bool:
        """True when no model turn may run for this message."""
        return self.action in ("reject", "ignore")


def resolve_policy(config: Any) -> MessagePolicyConfig | None:
    """The agent's ``message_policy``, or ``None`` when nothing is configured.

    ``None`` is the fast path every existing agent takes: callers skip the whole
    check, so an unconfigured deployment pays one ``getattr`` per message.

    The ``isinstance`` gate is not ceremony. Ingress reads ``runner.config``,
    which is not always an ``AgentConfig`` — a ``MagicMock`` in a runtime test,
    a duck-typed stand-in, a config unpickled from before this field existed —
    and on a mock every attribute is a truthy mock, so ``len(text) > limit``
    raises ``TypeError`` and takes the whole chat endpoint down with it. An
    object that cannot state a policy has none, which degrades to exactly the
    behaviour every agent had before this feature.
    """
    from olano.config.agent_config import MessagePolicyConfig as _MessagePolicyConfig

    policy = getattr(config, "message_policy", None)
    if not isinstance(policy, _MessagePolicyConfig):
        return None
    if not (
        policy.max_input_chars
        or policy.ignore_patterns
        or policy.ignore_rules
        or policy.max_output_tokens
    ):
        return None
    return policy


def _compiled(patterns: list[str]) -> tuple[re.Pattern[str], ...]:
    """Compile (and memoise) ``patterns``, dropping any that do not compile.

    A bad regex is skipped, never raised: this runs on the message path, and
    refusing every message because one pattern has an unbalanced paren is a
    worse failure than the rule not firing. ``MessagePolicyConfig`` rejects such
    a pattern at *save* time, which is where an operator can see and fix it —
    this branch only covers a config written before that validator existed or
    edited on disk by hand.
    """
    key = tuple(patterns)
    cached = _COMPILED.get(key)
    if cached is not None:
        return cached
    compiled: list[re.Pattern[str]] = []
    for pattern in patterns:
        try:
            compiled.append(re.compile(pattern, _PATTERN_FLAGS))
        except re.error as exc:
            if pattern not in _WARNED_BAD:
                _WARNED_BAD.add(pattern)
                logger.warning(
                    "message_policy.ignore_patterns: skipping %r — not a valid "
                    "regular expression (%s)",
                    pattern,
                    exc,
                )
    result = tuple(compiled)
    _COMPILED[key] = result
    return result


def default_oversize_notice(limit: int, actual: int) -> str:
    """Built-in text for a rejected over-long message.

    Names both numbers on purpose: "too long" alone leaves the sender guessing
    how much to cut, and they usually guess wrong and send twice.
    """
    return (
        f"⚠️ That message is {actual:,} characters — this assistant accepts up to "
        f"{limit:,}. Please shorten it and send again."
    )


def is_control_message(text: str) -> bool:
    """True for a slash command, which the policy never screens.

    Commands are the control surface (``/new``, ``/model``, ``/approve``), not
    conversation: they are dispatched by ``platforms.inbound.maybe_handle_command``
    and most never reach the model at all. Screening them would let a broad
    ``ignore_patterns`` entry lock an operator out of their own bot, and a
    ``max_input_chars`` of 500 silently break ``/olano`` with a long argument.
    Mirrors ``cli.commands.should_bypass_active_session``, which decides the
    same question one layer up.
    """
    return (text or "").strip().startswith("/")


#: How much of a message the regexes see. The size cap is checked *after* the
#: patterns on purpose (a spam blast that is also over-long must drop silently,
#: not get a "shorten your message" notice), which means the patterns run on
#: attacker-chosen length. A signature is in the first 20k characters or it is
#: not a signature; bounding the window keeps a linear pattern's cost linear in
#: something the operator controls.
PATTERN_SCAN_CHARS = 20_000


def screen_inbound(text: str, policy: MessagePolicyConfig | None) -> InboundVerdict:
    """Decide what happens to one inbound message, before any model call.

    ``text`` is the **sender's own words** — not the runtime's ``[from …]``
    prefix, not an untrusted-ingress envelope, not a transcribed attachment.
    Counting the prefix would make the cap depend on how long someone's display
    name is, and matching patterns against the envelope would let the runtime's
    own wording trip an operator's rule.

    Order matters: patterns are checked **before** the size cap, so a spam blast
    that is also over-long is dropped silently rather than answered with a
    "shorten your message" notice that confirms a live bot.
    """
    if policy is None:
        return InboundVerdict(action="allow", text=text)

    body = text or ""
    if is_control_message(body):
        return InboundVerdict(action="allow", text=body)

    patterns = _compiled(policy.ignore_patterns)
    window = body[:PATTERN_SCAN_CHARS]
    for pattern in patterns:
        if pattern.search(window):
            return InboundVerdict(
                action="ignore",
                text=body,
                notice=policy.ignore_notice,
                reason=f"ignore_pattern:{pattern.pattern}",
            )

    limit = policy.max_input_chars
    if limit and len(body) > limit:
        if policy.on_oversize == "truncate":
            return InboundVerdict(
                action="truncate",
                text=body[:limit],
                reason=f"oversize:{len(body)}>{limit}",
            )
        if policy.on_oversize == "ignore":
            return InboundVerdict(
                action="ignore",
                text=body,
                notice=policy.ignore_notice,
                reason=f"oversize:{len(body)}>{limit}",
            )
        return InboundVerdict(
            action="reject",
            text=body,
            notice=policy.oversize_notice.strip() or default_oversize_notice(limit, len(body)),
            reason=f"oversize:{len(body)}>{limit}",
        )

    return InboundVerdict(action="allow", text=body)


async def ascreen_inbound(text: str, policy: MessagePolicyConfig | None) -> InboundVerdict:
    """:func:`screen_inbound` for the async ingress seams.

    Python's ``re`` has no timeout, and a pattern with nested quantifiers
    (``^(a+)+$``) takes seconds on a **29-character** input. Patterns are
    operator config, so writing one is a self-inflicted slip — but the message
    is attacker-chosen, and ``screen_inbound`` runs synchronously inside the
    channel handler, so one slip plus one message froze the whole event loop:
    every agent, every channel, the dashboard, for as long as the backtracking
    took. Measured at 10 s for that pattern.

    So when there are patterns to run, run them on a worker thread. The stall
    still costs that thread — nothing in the stdlib can interrupt a regex — but
    it costs *only* that thread: the runtime keeps serving, the rate limiter
    bounds how many can be in flight, and the damage is one agent's throughput
    rather than the box. With no patterns configured the size cap is a
    ``len()`` and runs inline, so the common case pays no thread hop.
    """
    if policy is None or not policy.ignore_patterns or is_control_message(text or ""):
        return screen_inbound(text, policy)
    import asyncio

    return await asyncio.to_thread(screen_inbound, text, policy)


# ── The silence marker ───────────────────────────────────────────────────────


#: The marker as the model is asked to write it: alone on its line. Matched at
#: the reply's opening and at any later line start, never mid-sentence — an
#: agent *explaining* its policy ("the marker is `[[no_reply]]`") is not
#: declining, and a sender who pastes the marker cannot silence a reply that
#: was answering them. Leading spaces/tabs are tolerated; a model that indents
#: the line is still declining.
_MARKER_LINE = re.compile(r"(?m)^[ \t\r]*" + re.escape(SILENCE_MARKER))

#: Whitespace that may precede the marker on its line. Deliberately not `\n`:
#: a newline *ends* a line, so a blank line before the marker is handled by the
#: line-start logic, not by stripping.
_LINE_WS = " \t\r"


def is_silenced(reply: str) -> bool:
    """True when ``reply`` carries the model's decline.

    Two shapes count, and both are needed. The prompt asks for the marker as
    the **entire** reply, and a compliant model does that. But models also
    preface it ("Let me check that.\n\n[[no_reply]]") — and that is exactly
    the case where Hermes and OpenClaw both shipped the marker verbatim into a
    customer's chat. So the marker on **its own line, anywhere** is a decline
    too. The cost is the rare model that answers a question and then appends
    the marker anyway: that turn is dropped, which the operator sees as a note
    on the dashboard and can tune. The alternative — a bare ``[[no_reply]]``
    in a WhatsApp thread — is the one outcome this feature must never produce.
    """
    return bool(reply) and _MARKER_LINE.search(reply) is not None


def strip_marker(reply: str) -> str:
    """``reply`` with every marker line removed — for a surface that decides to
    show the text around a decline (the operator's console) rather than drop
    it. Empty when the marker was the whole reply."""
    if not reply:
        return ""
    return _MARKER_LINE.sub("", reply).strip()


class SilenceFilter:
    """Keeps :data:`SILENCE_MARKER` off the wire while the turn decides.

    A consumer feeds every text chunk through :meth:`feed` and emits what comes
    back. The filter is **armed at the reply's opening and at the start of
    every line**: while the text since the last line start could still become
    the marker it is held (at most ``len(SILENCE_MARKER)`` characters plus that
    line's leading whitespace); the moment it cannot, it is released in one
    piece and the filter passes text through until the next newline. So a
    normal reply pays one ``startswith`` on a ≤12-char probe per line, and a
    code block full of newlines costs a few hundred of those — nothing a stream
    can feel.

    Re-arming per line is what closes the leak :func:`is_silenced` describes:
    the marker after a preamble is caught at its line start, streamed to
    nobody, and reported like a marker at the opening. Once :attr:`silenced` is
    set, ``feed`` returns ``""`` for the rest of the turn — a model that
    declines and then keeps talking must not have the tail delivered.

    :meth:`flush` closes out the turn: a reply shorter than the marker ("Hi")
    would otherwise end with its text still held, and a turn cut off *inside*
    the marker (``[[no_repl`` — ``max_output_tokens``, a stop, a provider
    hiccup) counts as the decline it was becoming rather than leaking a broken
    marker. Whitespace alone is not a fragment: reporting it as a policy hit
    would misattribute an empty turn.
    """

    __slots__ = ("_armed", "_held", "silenced")

    def __init__(self) -> None:
        self._held = ""
        self._armed = True
        self.silenced = False

    def feed(self, chunk: str) -> str:
        """Text safe to emit now — ``""`` while buffering or once silenced."""
        if self.silenced:
            return ""
        buf = self._held + chunk
        self._held = ""
        out: list[str] = []
        while buf:
            if self._armed:
                probe = buf.lstrip(_LINE_WS)
                if probe.startswith("\n"):
                    # A blank line. Pass it through and stay armed: the marker
                    # may open the next one ("\n\n[[no_reply]]" is the common
                    # preamble shape).
                    n = len(buf) - len(probe) + 1
                    out.append(buf[:n])
                    buf = buf[n:]
                    continue
                if probe.startswith(SILENCE_MARKER):
                    self.silenced = True
                    self._armed = False
                    return "".join(out)
                if SILENCE_MARKER.startswith(probe):
                    # Still a possible prefix — including the empty probe, i.e.
                    # nothing but spaces so far on this line. Hold it.
                    self._held = buf
                    return "".join(out)
                self._armed = False  # this line is ordinary text
            i = buf.find("\n")
            if i < 0:
                out.append(buf)
                buf = ""
            else:
                out.append(buf[: i + 1])
                buf = buf[i + 1 :]
                self._armed = True
        return "".join(out)

    def flush(self) -> str:
        """Emit anything still held at end of turn, and stop buffering."""
        if self.silenced:
            return ""
        held, self._held = self._held, ""
        if self._armed:
            probe = held.lstrip(_LINE_WS)
            if probe and SILENCE_MARKER.startswith(probe):
                self.silenced = True
                return ""
        self._armed = False
        return held


def declined_reply(
    collected: list[tuple[str, Any]], policy: MessagePolicyConfig | None
) -> str | None:
    """What a channel handler should send for a turn the agent declined.

    ``None`` — the turn was not declined; render it as usual.
    ``""`` — declined, send nothing.
    a string — declined, send this one line (the operator's ``ignore_notice``).

    ``collected`` is the turn's ``astream_events`` output. The decision keys on
    the runner's ``("message_policy", …)`` event, never on the text, because by
    the time this runs the filter has already kept the marker out of the text.

    **Ask this before ``render_events``, never after.** In ``normal`` verbosity
    a turn with no text but some tool calls renders ``(done — N tool call(s):
    …)``, so a handler that rendered first would answer a message the agent
    chose not to answer — with a line describing the tools it used to decide.
    Both channel handlers in ``runtime/server.py`` call this first; the pin in
    ``tests/test_message_policy.py`` counts the calls.
    """
    if not any(kind == "message_policy" for kind, _ in collected):
        return None
    return (policy.ignore_notice if policy is not None else "") or ""


# ── Prompt ───────────────────────────────────────────────────────────────────

_PROMPT_HEADER = "## Reply policy"


def policy_prompt_block(policy: MessagePolicyConfig | None) -> str:
    """The ``ignore_rules`` block for the system prompt, or ``""``.

    Injected by ``core/prompt_assembler.build_l0`` as a code-owned block —
    alongside the channel-trust rules and for the same reason: an operator who
    configured silence rules must get them whether or not their archetype's
    prompt happens to mention the subject.

    Only ``ignore_rules`` appear here. Patterns and the size caps are enforced
    before the model runs, so telling it about them would spend tokens
    describing something it can neither observe nor influence.
    """
    if policy is None or not policy.ignore_rules:
        return ""
    rules = "\n".join(f"- {rule}" for rule in policy.ignore_rules)
    return (
        f"{_PROMPT_HEADER}\n"
        "These conditions apply to **messages people send you on a chat "
        "channel** (Telegram, WhatsApp, the web chat, and so on). They never "
        "apply to a scheduled task, a heartbeat, a system instruction, or a "
        "request from another agent — always carry those out normally.\n"
        "Before answering a person's message, check it against the conditions "
        "below. If **any** of them is true, you must not reply to it:\n"
        f"{rules}\n\n"
        f"To decline, your entire reply must be exactly `{SILENCE_MARKER}` — no "
        "explanation, no apology, no greeting, and no tool calls first. The "
        "runtime recognises that marker and sends nothing, so writing anything "
        "beside it delivers that text to the sender instead of staying silent.\n"
        "If none of the conditions is true, answer normally and never mention "
        "this policy or the marker."
    )


def agent_prompt_block(config: AgentConfig | Any) -> str:
    """:func:`policy_prompt_block` for an agent config (prompt-assembler seam)."""
    return policy_prompt_block(resolve_policy(config))
