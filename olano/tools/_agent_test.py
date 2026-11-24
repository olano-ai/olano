"""``olano_test_agent``: AgentFather driving another agent with a test message.

AgentFather builds agents, edits their instructions and wires their tools, and
until this it had no honest way to check the result: answering "the new agent
will handle that" is a prediction, not a test. This runs a real turn of the
target agent on a separate test thread and hands back what that turn said and
which tools it called, so AgentFather can check the behaviour, adjust the
agent and try again, then report to the administrator who asked.

**Who may start one.** The tool lives in the ``olano_admin`` toolkit (granted
to AgentFather only, a privileged toolkit nobody can grant themselves), is
``owner_only``, and calls ``_human_gate.admin_human_denial``: an administrator
must be on the turn, never a peer, a timer, a webhook or an outside caller.
The tool sits in HITL's ``cross_agent_exec`` category, next to
``olano_scheduler_run_now`` (which also makes another agent take a full turn on
a supplied prompt): every test raises an approval card naming the agent and the
message, approved individually, never by a remembered rule or a blanket
approval.

**The test turn is not the administrator.** It runs as ``source="agent_test"``
with ``AGENT_ADMIN_KEY`` forced ``False``. ``_ownership_gate`` and
``admin_human_denial`` both list that source among the no-human ones, so the
agent under test cannot reconfigure itself, publish itself, or start a test of
its own. Its origin is the administrator's turn propagated one hop, so an
action that needs approval reaches the approvals inbox as it would for a
delegated turn; actions that need none run for real. This is a test of the real
agent, not a dry run.

**Test threads are their own namespace.** Every id is ``agenttest::<suffix>``
under the target agent's sessions; an id passed without the prefix gets it, so
a test can never be pointed at a customer's WhatsApp thread or an owner's chat.
The prefix is a system thread prefix everywhere it matters (``thread_labels``,
``talk_to``, ``analytics.journal``) and fact distillation skips it, so test
inputs never become memory.

The default run reuses the target's live runner, so the test sees the agent
exactly as it is serving. A ``system_prompt`` override, ``reload_config``, or a
target that is not running builds a transient copy instead (the pattern of the
dashboard's per-turn model override, ``AgentRunner._agent_for_model``): same
name, so the same checkpointer, transcripts and workspace, and one thread can
alternate between the live agent and an override without losing its history.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from typing import Any

from olano.core.thread_labels import AGENT_TEST_THREAD_PREFIX as THREAD_PREFIX

logger = logging.getLogger(__name__)

TEST_SOURCE = "agent_test"

DEFAULT_TIMEOUT_SECONDS = 600
MAX_TIMEOUT_SECONDS = 1800
MIN_TIMEOUT_SECONDS = 10

MAX_MESSAGE_CHARS = 20_000
MAX_SYSTEM_PROMPT_CHARS = 100_000

SYSTEM_PROMPT_MODES = ("instructions", "full")

# Trace bounds: enough to see what a tool was given and what it returned,
# without one chatty tool flooding the calling turn's context.
_TRACE_MAX_CALLS = 40
_TRACE_ARGS_CHARS = 800
_TRACE_RESULT_CHARS = 1500

# The suffix after the prefix. No slash or dot-dot can reach a path: thread ids
# become ``<thread>.jsonl`` file names under the agent's session directory.
_SUFFIX_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}$")

# (agent, thread) pairs with a test turn running. Two turns on one thread at
# once would interleave in the checkpoint; the second caller is refused.
_INFLIGHT: set[tuple[str, str]] = set()


def normalize_thread_id(raw: str | None) -> tuple[str, bool, str | None]:
    """``(thread_id, is_new, error)`` for a requested test thread.

    Empty ⇒ a fresh ``agenttest::<12 hex>``. Anything else is put in the test
    namespace (prefixed when it is not already) and validated; ``error`` is a
    message for the model when the suffix is unusable.
    """
    value = (raw or "").strip()
    if not value:
        return f"{THREAD_PREFIX}{uuid.uuid4().hex[:12]}", True, None
    suffix = value[len(THREAD_PREFIX) :] if value.startswith(THREAD_PREFIX) else value
    if not _SUFFIX_RE.match(suffix):
        return (
            "",
            False,
            "thread_id must be one a previous olano_test_agent returned (agenttest::…), or a "
            "short name of letters, digits, '.', '_', ':' or '-' (at most 96 characters)",
        )
    return f"{THREAD_PREFIX}{suffix}", False, None


def _clip(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"


def _message_text(msg: Any) -> str:
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content or "")


def trace_from_messages(messages: list[Any]) -> list[dict[str, Any]]:
    """The tool calls of the last turn in ``messages``, with their results.

    "The last turn" is everything after the last human message. That is the
    test turn: it carries exactly one human message (system notices are
    ``system``), and reading backwards from the end is unaffected by a
    summarization that rewrote the earlier history mid-turn.
    """
    start = 0
    for idx in range(len(messages) - 1, -1, -1):
        if getattr(messages[idx], "type", "") == "human":
            start = idx + 1
            break
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    for msg in messages[start:]:
        kind = getattr(msg, "type", "")
        if kind == "ai":
            for call in getattr(msg, "tool_calls", None) or []:
                if len(calls) >= _TRACE_MAX_CALLS:
                    break
                try:
                    args = json.dumps(call.get("args") or {}, ensure_ascii=False, default=str)
                except Exception:
                    args = str(call.get("args"))
                entry: dict[str, Any] = {
                    "tool": call.get("name") or "",
                    "args": _clip(args, _TRACE_ARGS_CHARS),
                    "result": None,
                }
                calls.append(entry)
                if call.get("id"):
                    by_id[str(call["id"])] = entry
        elif kind == "tool":
            paired = by_id.get(str(getattr(msg, "tool_call_id", "") or ""))
            if paired is None:
                continue
            paired["result"] = _clip(_message_text(msg), _TRACE_RESULT_CHARS)
            if getattr(msg, "status", "success") == "error":
                paired["status"] = "error"
    return calls


async def _turn_trace(
    runner: Any, thread_id: str, *, expect_human: str | None = None
) -> list[dict[str, Any]] | None:
    """Read the test turn's tool calls back out of the checkpoint (best effort).

    ``expect_human`` is for a turn that failed: it may have died before its
    message reached the checkpoint, and then "after the last human message" is
    the *previous* turn. Its calls are reported only when the last human
    message is this one; a completed turn needs no such check.
    """
    try:
        state = await runner.agent.aget_state(runner._invoke_config(thread_id))
        values = getattr(state, "values", None) or {}
        messages = list(values.get("messages") or [])
        if expect_human is not None:
            last_human = next(
                (m for m in reversed(messages) if getattr(m, "type", "") == "human"), None
            )
            if last_human is None or _message_text(last_human).strip() != expect_human:
                return None
        return trace_from_messages(messages)
    except Exception:
        logger.debug("olano_test_agent: could not read the turn's tool calls", exc_info=True)
        return None


def _count_user_turns(runner: Any, thread_id: str) -> int:
    try:
        return sum(1 for e in runner.load_session(thread_id) if e.get("role") == "user")
    except Exception:
        return 0


def resolve_target(name: str) -> tuple[str | None, Any | None, str | None]:
    """``(canonical_name, live_runner_or_None, error)`` for the agent to test.

    A running agent resolves case-insensitively against the runtime registry,
    like ``talk_to_agent``. One that is configured but not running resolves to
    its on-disk name with no runner (the test builds it). Anything else is an
    error that lists what exists.
    """
    from olano.tools.talk_to import get_bound_runtime

    wanted = (name or "").strip()
    if not wanted:
        return None, None, "agent is required"
    runtime = get_bound_runtime()
    runners = (getattr(runtime, "_runners", None) or {}) if runtime else {}
    if wanted in runners:
        return wanted, runners[wanted], None
    for key, runner in runners.items():
        if key.casefold() == wanted.casefold():
            return key, runner, None
    from olano.config.loader import find_agent_config_path, list_agents

    if find_agent_config_path(wanted) is not None:
        return wanted, None, None
    known = sorted(set(runners) | set(list_agents()))
    return None, None, f"no agent named '{wanted}'; agents: {', '.join(known) or '(none)'}"


async def _resolve_runner(
    agent_name: str,
    live: Any | None,
    *,
    system_prompt: str | None,
    system_prompt_mode: str,
    reload_config: bool,
) -> tuple[Any, str]:
    """``(runner, config_label)`` the test turn runs on.

    The live runner when nothing is overridden; otherwise a transient copy built
    from the live config (or the on-disk one, when asked or when the agent is
    not running).
    """
    if live is not None and system_prompt is None and not reload_config:
        return live, "live"

    from olano.core.builder import build_agent_async, build_agent_with_mcp
    from olano.core.prompt_state import PromptState
    from olano.core.runner import AgentRunner

    if reload_config or live is None:
        from olano.config.loader import load_agent_config

        config = await asyncio.to_thread(load_agent_config, agent_name)
        label = "reloaded from disk" if live is not None else "built from disk (not running)"
    else:
        config = live.config
        label = "live"

    full_override: str | None = None
    if system_prompt is not None:
        if system_prompt_mode == "full":
            full_override = system_prompt
        else:
            config = config.model_copy(update={"system_prompt": system_prompt})

    graph = await (
        build_agent_with_mcp(config, system_prompt_override=full_override)
        if config.mcp_servers
        else build_agent_async(config, system_prompt_override=full_override)
    )
    runner = AgentRunner(graph, config)
    # A test copy's prompt is deliberately not the agent's own, so the "your
    # instructions changed" notice would only be noise in the test thread (and
    # would advance the thread's marker against a prompt that never shipped).
    runner._prompt_state = PromptState()
    return runner, label


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"status": "error", "error": message, **extra}


async def run_agent_test(
    agent: str,
    *,
    caller: str,
    user_message: str,
    thread_id: str | None = None,
    system_prompt: str | None = None,
    system_prompt_mode: str = "instructions",
    reload_config: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run one test turn of ``agent`` and describe it. Never raises.

    ``caller`` is the agent asking (AgentFather): it is appended to the
    propagated origin's call path, and an agent may not test itself.
    """
    message = (user_message or "").strip()
    if not message:
        return _error("user_message is required")
    if len(message) > MAX_MESSAGE_CHARS:
        return _error(
            f"user_message is {len(message)} characters; the limit is {MAX_MESSAGE_CHARS}"
        )
    mode = (system_prompt_mode or "instructions").strip().lower()
    if mode not in SYSTEM_PROMPT_MODES:
        return _error(f"system_prompt_mode must be one of {', '.join(SYSTEM_PROMPT_MODES)}")
    if system_prompt is not None:
        if not system_prompt.strip():
            return _error(
                "system_prompt is empty; omit it to test with the agent's configured prompt"
            )
        if len(system_prompt) > MAX_SYSTEM_PROMPT_CHARS:
            return _error(
                f"system_prompt is {len(system_prompt)} characters; "
                f"the limit is {MAX_SYSTEM_PROMPT_CHARS}"
            )
    tid, is_new, tid_error = normalize_thread_id(thread_id)
    if tid_error:
        return _error(tid_error)
    try:
        timeout = int(timeout_seconds)
    except (TypeError, ValueError):
        timeout = DEFAULT_TIMEOUT_SECONDS
    timeout = max(MIN_TIMEOUT_SECONDS, min(MAX_TIMEOUT_SECONDS, timeout))

    target, live, target_error = resolve_target(agent)
    if target is None:
        return _error(target_error or "unknown agent")
    if caller and target.casefold() == caller.casefold():
        return _error(
            "an agent cannot test itself with this tool; name another agent",
        )

    key = (target, tid)
    if key in _INFLIGHT:
        return _error(
            "a test turn is already running on this thread; wait for it to finish",
            agent=target,
            thread_id=tid,
        )
    _INFLIGHT.add(key)
    started = time.monotonic()
    try:
        return await _run(
            target,
            live,
            caller=caller,
            message=message,
            thread_id=tid,
            is_new=is_new,
            system_prompt=system_prompt,
            mode=mode,
            reload_config=reload_config,
            timeout=timeout,
            started=started,
        )
    finally:
        _INFLIGHT.discard(key)


async def _run(
    agent_name: str,
    live: Any | None,
    *,
    caller: str,
    message: str,
    thread_id: str,
    is_new: bool,
    system_prompt: str | None,
    mode: str,
    reload_config: bool,
    timeout: int,
    started: float,
) -> dict[str, Any]:
    from olano.security.request_context import (
        AGENT_ADMIN_KEY,
        HIDDEN_PROMPT_KEY,
        ORIGIN_KEY,
        propagated_origin,
        push_request_context,
    )
    from olano.tools.talk_to import current_agent

    result: dict[str, Any] = {"agent": agent_name, "thread_id": thread_id, "new_thread": is_new}
    try:
        runner, config_label = await _resolve_runner(
            agent_name,
            live,
            system_prompt=system_prompt,
            system_prompt_mode=mode,
            reload_config=reload_config,
        )
    except Exception as exc:
        logger.info("olano_test_agent: building a test copy of %s failed: %s", agent_name, exc)
        result.update(
            status="error",
            stage="build",
            error=f"could not build '{agent_name}' for the test: {type(exc).__name__}: {exc}",
        )
        return result

    result["turn"] = _count_user_turns(runner, thread_id) + 1
    result["ran_with"] = {
        "config": config_label,
        "system_prompt": ("configured" if system_prompt is None else f"override ({mode})"),
    }

    status = "completed"
    output = ""
    token = current_agent.set(agent_name)
    try:
        with push_request_context(
            source=TEST_SOURCE,
            channel=TEST_SOURCE,
            metadata={
                # The administrator's turn, one hop further: an approval the
                # test turn needs reaches the approvals inbox, as it would for
                # a delegated turn.
                ORIGIN_KEY: propagated_origin(caller),
                # The agent under test is not the administrator. Both gates
                # refuse the source anyway; this keeps a future reader of the
                # key honest.
                AGENT_ADMIN_KEY: False,
                # The caller's metadata merges into this context. None of these
                # belong to the test turn.
                HIDDEN_PROMPT_KEY: False,
                "originator_user_thread": "",
                "a2a_preamble": "",
            },
        ):
            output = await asyncio.wait_for(runner.ainvoke(message, thread_id=thread_id), timeout)
    except TimeoutError:
        status = "timeout"
        result["error"] = (
            f"the test turn did not finish within {timeout}s and was stopped; "
            "anything it completed before that is kept on the thread"
        )
    except Exception as exc:
        status = "error"
        result["stage"] = "turn"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        current_agent.reset(token)

    result["status"] = status
    result["output"] = output or ""
    trace = await _turn_trace(
        runner, thread_id, expect_human=None if status == "completed" else message
    )
    if trace is not None:
        result["tool_calls"] = trace
    result["duration_seconds"] = round(time.monotonic() - started, 2)
    result["next"] = (
        f'Pass thread_id="{thread_id}" with agent="{agent_name}" to continue this '
        "conversation; omit thread_id to start a fresh one."
    )
    return result
