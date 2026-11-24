"""Run agents and stream responses, with session JSONL persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from olano.analytics.usage_recorder import (
    ensure_streamed_credits,
    merge_olano_credits,
    merge_result_usage,
    merge_usage_metadata,
    turn_origin_label,
)
from olano.config.agent_config import AgentConfig
from olano.config.workspace import get_workspace
from olano.core.actionable_errors import (
    CODE_GENERIC,
    ActionableError,
    classify_exception,
)
from olano.core.checkpoints import scope_thread
from olano.core.stream_isolation import detached_runnable_config, has_nostream_tag
from olano.core.thread_labels import channel_for_request
from olano.core.tool_failures import (
    TURN_TOOL_FAILURES,
    WRITE_TOOL_NAMES,
    TurnToolFailures,
    is_failed_tool_result,
    strip_repeat_failure_notice,
    tool_call_signature,
)
from olano.security.egress_guard import exempt as egress_exempt
from olano.security.hitl_policy import CATEGORY_TOOLS
from olano.security.output_scrub import scrub_text
from olano.security.request_context import (
    HIDDEN_PROMPT_KEY,
    current_origin,
    get_request_context,
    push_request_context,
)
from olano.tools.shell import classify_shell_command_risk

logger = logging.getLogger(__name__)

# Session JSONL schema version. Bump when the on-disk shape changes.
# Lines without this field are treated as legacy v0 (pre-2026-05).
SESSION_SCHEMA_VERSION = 1

_TRUNCATION_CONTINUE_PROMPT = "[Your previous response was truncated. Continue.]"

# Cap on per-runner hook-context entries (one per distinct thread_id). The value
# is a small deterministic dict, so an LRU cap bounds the count with no behaviour
# change — an evicted thread just rebuilds the identical context on next use.
_HOOK_CONTEXTS_MAX = 512

# Context-only conversation events (record_context_note): queued in
# <thread>.state.json under this key and injected as a one-shot system message
# at the start of the thread's next turn. Bounded so a chat that never runs
# again can't grow its state file forever — the JSONL transcript keeps the
# full record either way.
PENDING_CONTEXT_NOTES_KEY = "pending_context_notes"
_CONTEXT_NOTES_MAX = 20
_CONTEXT_NOTES_HEADER = (
    "[Context updates: the following happened in this conversation since your "
    "previous turn. They are shown for awareness only — each was already "
    "sent or handled, so do not reply to or act on them directly.]"
)
_BUDGET_NOTICE_70 = (
    "[I've used most of the allowed tool steps for this reply, so I'm going to start wrapping up.]"
)
_BUDGET_NOTICE_90 = "[I'm almost at the tool-step limit for this reply, so I'll finish up now.]"
_PIP_REQUIREMENT_RE = re.compile(
    r"(?:No matching distribution found for|Could not find a version that satisfies the requirement)\s+([^\s)]+)",
    re.IGNORECASE,
)
_EXPLICIT_ASK_PEER_RE = re.compile(
    r"\bask\s+([A-Za-z][A-Za-z0-9_-]{1,63})\b",
    re.IGNORECASE,
)
_PEER_TOOL_NAMES = {"talk_to_agent", "talk_to"}

# Read-only sentinel returned in place of a real graph state when no interrupt is
# possible this iteration, so the hot path can avoid deserializing the checkpoint.
# Never mutated — ``interrupts`` is falsy and ``values`` is empty.
_NO_INTERRUPT_STATE = SimpleNamespace(interrupts=None, values={})


def _truncate_session_debug_text(value: str, limit: int = 1200) -> str:
    text = value.strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _truncate_security_log_preview(value: str, limit: int = 512) -> str:
    """Single-line, length-capped text safe to emit in security log lines."""
    text = " ".join(value.split())
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _pii_redacted_preview_for_log(raw: str, processed: str, pii_action: str) -> str:
    """Build a redacted preview for logs (covers warn/redact; never log raw PII)."""
    from olano.security.pii_middleware import redact as pii_redact

    safe = processed if pii_action == "redact" else pii_redact(raw).cleaned
    return _truncate_security_log_preview(safe)


def _log_inbound_agent_message(config: AgentConfig, processed_text: str) -> None:
    """Log non-local inbound turns so operators can correlate channels/API with agents."""
    ctx = get_request_context()
    if ctx.source == "local":
        return
    logger.info(
        "Inbound agent message [agent=%s source=%s channel=%s user=%s session=%s chars=%d]",
        config.name,
        ctx.source,
        ctx.channel,
        ctx.user_id,
        ctx.session_id or "-",
        len(processed_text),
    )


class AgentError(Exception):
    """Friendly agent error with a user-visible message.

    ``detail`` carries the structured :class:`ActionableError` (code +
    remediation) when one was classified, so every surface — dashboard chat,
    chat channels — can render the same actionable guidance instead of a bare
    string.
    """

    def __init__(
        self,
        message: str,
        original: Exception | None = None,
        detail: ActionableError | None = None,
    ):
        super().__init__(message)
        self.original = original
        self.detail = detail


# ─────────────────────────── Error classification registry ───────────────────
# The structured rules (codes + remediation) live in
# :mod:`olano.core.actionable_errors`; ``_classify`` delegates there. The
# provider-rule registry below remains as a thin override layer: callers can
# register a provider-specific *message* that wins over the structured default
# (e.g. an OpenAI-shaped ``insufficient_quota`` wording). Predicates run against
# the lower-cased exception ``str()`` for cheap substring matching.

from collections.abc import Callable

_ErrorPredicate = Callable[[str, Exception], bool]
_ErrorRender = Callable[[Exception], str] | str

_PROVIDER_RULES: dict[str, list[tuple[_ErrorPredicate, _ErrorRender]]] = {}


def _has_any(*needles: str) -> _ErrorPredicate:
    def _pred(msg: str, _exc: Exception) -> bool:
        return any(n in msg for n in needles)

    return _pred


def _is_codex_stream_tail_parse_error(exc: Exception, provider: str | None = None) -> bool:
    """Return True for a known LangChain Responses parser tail error.

    The ChatGPT-account Codex backend can stream valid text and then emit a
    terminal event shape that langchain-openai currently trips over with
    ``TypeError: 'NoneType' object is not iterable``. Treating that as fatal
    discards an otherwise successful response after the user has already seen
    the text.
    """
    if provider != "openai-codex" or not isinstance(exc, TypeError):
        return False
    return "'nonetype' object is not iterable" in str(exc).lower()


def _resolved_model(model: str | None) -> str | None:
    """The concrete model behind a possible ``default:*`` alias.

    Billing, credits estimation and provider-keyed error classification all
    fork on the model string, and none of them knows the alias layer — an
    unresolved ``default:turbo`` would meter as an unknown BYOK model and
    classify errors under a nonexistent "default" provider. Never raises: an
    unresolvable alias (unknown tier in a hand-edited file) returns verbatim,
    because recording something beats dropping the row.
    """
    if not model:
        return model
    try:
        from olano.models.aliases import resolve_model_alias

        return resolve_model_alias(model)
    except Exception:
        return model


def _provider_from_model(model: str | None) -> str | None:
    model = _resolved_model(model)
    if not model or ":" not in model:
        return None
    provider, _, _ = model.partition(":")
    return provider or None


def register_provider_error_rules(
    provider: str,
    rules: list[tuple[_ErrorPredicate, _ErrorRender]],
) -> None:
    """Register provider-specific error rules.

    A matching provider rule's message overrides the structured default for that
    provider (the structured remediation is still attached when one is known).
    """
    _PROVIDER_RULES.setdefault(provider, []).extend(rules)


def reset_provider_error_rules() -> None:
    """Forget all registered provider rules (used by tests)."""
    _PROVIDER_RULES.clear()


def _classify(exc: Exception, provider: str | None = None) -> ActionableError:
    """Classify *exc* into a structured :class:`ActionableError`.

    Order: the known Codex stream-tail false-positive → provider-registered
    message overlays → the structured classifier → a generic fallback.
    """
    if _is_codex_stream_tail_parse_error(exc, provider):
        return ActionableError(
            "codex_stream_tail",
            "OpenAI Codex stream ended with an unsupported terminal event.",
            detail=str(exc),
        )
    msg = str(exc).lower()

    # Provider-registered message overlay wins on text, but still inherits the
    # structured remediation/code when the classifier recognizes the error.
    if provider and provider in _PROVIDER_RULES:
        for predicate, render in _PROVIDER_RULES[provider]:
            if predicate(msg, exc):
                text = render(exc) if callable(render) else render
                structured = classify_exception(exc, provider)
                return ActionableError(
                    structured.code if structured else CODE_GENERIC,
                    text,
                    remediation=structured.remediation if structured else None,
                    retryable=bool(structured and structured.retryable),
                    detail=str(exc),
                )

    structured = classify_exception(exc, provider)
    if structured is not None:
        return structured

    name = type(exc).__name__
    return ActionableError(CODE_GENERIC, f"API error ({name}): {exc}", detail=str(exc))


def _classify_error(exc: Exception, provider: str | None = None) -> str:
    """Back-compat string view of :func:`_classify` (used widely + in tests)."""
    return _classify(exc, provider).message


def _agent_error(exc: Exception, provider: str | None = None) -> AgentError:
    """Wrap *exc* in an :class:`AgentError` carrying its structured classification."""
    classified = _classify(exc, provider)
    return AgentError(classified.message, original=exc, detail=classified)


# Built-in OpenAI overlay — covers the only divergence we know about today
# (``insufficient_quota`` rather than Anthropic's "credit balance" wording).
register_provider_error_rules(
    "openai",
    [
        (
            _has_any("insufficient_quota", "billing"),
            (
                "OpenAI billing limit reached. "
                "Visit https://platform.openai.com/account/billing to top up."
            ),
        ),
    ],
)


# ─────────────────────────────────── Security helpers ────────────────────────

_WEB_TOOL_NAMES = frozenset(
    {
        "web_search",
        "page_extract",
        "web_extract_deep",
        "web_extract",
        "page_read",
        "web_fetch_fast",
        "web_fetch",
        "url_download",
        "web_download_file",
        "web_download",
        "page_screenshot",
        "image_describe",
        "http_request",
        "http_fetch_api",
        "http_fetch",
        "http_download",
        "firecrawl_scrape",
        "firecrawl_crawl",
        "firecrawl_map",
        "firecrawl_extract",
        "browser_extract",
        "browser_navigate",
    }
)

# Tool names that require shell-guard validation (a separate axis from HITL —
# this is command sanitization, not approval).
_SHELL_TOOL_NAMES = frozenset(
    {"safe_shell", "safe_shell_tool", "node_run", "olano_run_agent_command"}
)

# HITL-relevant tool sets are derived from the single source of truth in
# hitl_policy so the builder's interrupt_on map and the runtime risk classifier
# can never drift apart.
_INTERRUPTIBLE_SHELL_TOOL_NAMES = CATEGORY_TOOLS["shell"]
_WRITE_TOOL_NAMES = CATEGORY_TOOLS["write_external"] | CATEGORY_TOOLS["write_fs"]
_SECRET_TOOL_NAMES = CATEGORY_TOOLS["secret_read"] | CATEGORY_TOOLS["secret_write"]
_CROSS_AGENT_EXEC_TOOL_NAMES = CATEGORY_TOOLS["cross_agent_exec"]


def _always_asks_human(action: dict[str, Any]) -> bool:
    """An approval only a person may grant, whatever mode or origin the turn has.

    A browser tool raises an approval only for an action it judged
    consequential: submitting a form that is not a lookup, or a control
    labelled buy / pay / send / post / delete (``olano.browser.consequence``).
    Those are exactly the actions a prompt injected by a web page would aim
    for, so neither the "auto"/"act" chat modes, a remembered "always allow",
    the low-risk switch, a scheduled or background run, nor a messenger's
    trusted-sender shortcut may approve one: each would let the page's text
    approve its own request.
    """
    from olano.browser.policy import ALWAYS_ASK_TOOL_NAMES

    return str(action.get("name", "")) in ALWAYS_ASK_TOOL_NAMES


_ALWAYS_ALLOWED_PROFILE_TOOLS = frozenset(
    {"user_profile_save", "identity_preference_save", "soul_preference_save"}
)


def _validate_shell_tool_call(tool_name: str, args_raw: str, config: AgentConfig) -> str | None:
    """
    Validate a shell tool call with shell_guard.
    Returns a block reason string if blocked, None if safe.
    """
    if tool_name not in _SHELL_TOOL_NAMES:
        return None
    try:
        from olano.security.shell_guard import validate_command

        args = json.loads(args_raw) if args_raw.startswith("{") else {}
        command = args.get("command", args_raw)
        is_safe, reason = validate_command(command)
        if not is_safe:
            logger.warning(
                "ShellGuard: blocked command [agent=%s]: %s — %s",
                config.name,
                command[:80],
                reason,
            )
            return reason
    except Exception as e:
        logger.debug("ShellGuard: validation skipped: %s", e)
    return None


def _scan_web_tool_result(tool_name: str, content: str, config: AgentConfig) -> str:
    """Apply WebScan middleware to results from web-fetching tools."""
    if tool_name not in _WEB_TOOL_NAMES:
        return content
    try:
        from olano.security.web_scan import apply_web_scan

        action = getattr(config.security, "web_scan_action", "clean")
        cleaned, injections = apply_web_scan(content, action=action)
        if injections:
            logger.warning(
                "WebScan: detected %d injection pattern(s) in %s result [agent=%s]: %s",
                len(injections),
                tool_name,
                config.name,
                injections[:3],
            )
        return cleaned
    except Exception as e:
        logger.debug("WebScan: skipped for %s: %s", tool_name, e)
    return content


def _scan_input(text: str, config: AgentConfig) -> str:
    """
    Run PII + injection scanning on user input.
    Returns (possibly redacted) text. Raises AgentError if blocked.
    """
    sec = config.security
    try:
        from olano.security.input_scan import scan_input
        from olano.security.pii_middleware import apply as pii_apply

        # Injection scan
        result = scan_input(text)
        if not result.is_safe:
            findings_str = "; ".join(f["category"] for f in result.findings)
            logger.warning(
                "Security: input scan flagged message [agent=%s risk=%s]: %s",
                config.name,
                result.risk_level,
                findings_str,
            )
            if result.risk_level == "high" and sec.profile == "production":
                raise AgentError(
                    f"Message blocked: potential prompt injection detected ({findings_str})."
                )

        # PII scan — skipped for agent↔agent (talk_to_agent) exchanges unless
        # the agent opts back in via `security.pii_scan_peer` (redacting or
        # blocking peer hand-offs breaks collaboration; the human boundary is
        # still scanned).
        from olano.security.pii_middleware import peer_exchange_exempt

        if sec.pii_scan_input and not peer_exchange_exempt(sec):
            cleaned, findings = pii_apply(text, sec.pii_action)
            if findings:
                types = {f["type"] for f in findings}
                ctx = get_request_context()
                preview = _pii_redacted_preview_for_log(text, cleaned, sec.pii_action)
                logger.warning(
                    "Security: PII detected in input [agent=%s types=%s action=%s "
                    "source=%s channel=%s user=%s session=%s redacted_preview=%s]",
                    config.name,
                    types,
                    sec.pii_action,
                    ctx.source,
                    ctx.channel,
                    ctx.user_id,
                    ctx.session_id or "-",
                    preview,
                )
            _log_inbound_agent_message(config, cleaned)
            return cleaned
    except AgentError:
        raise
    except Exception as e:
        logger.debug("Security: input scan skipped: %s", e)
    _log_inbound_agent_message(config, text)
    return text


def _needs_paragraph_break(full_text: list[str]) -> bool:
    """Whether answer text resuming after a tool boundary needs a blank line first.

    Chunks are concatenated verbatim by every consumer (the dashboard's
    ``m.body += text``, the messenger renderers' ``"".join``, the persisted
    JSONL row), so "…are drawn." followed by "Found it —" used to read as one
    sentence. True when something was already said and it does not end in a
    newline of its own.
    """
    if not full_text:
        return False
    tail = "".join(full_text[-2:])
    return not tail.endswith("\n")


def _scan_output(text: str, config: AgentConfig) -> str:
    """
    Run PII + credential leak scanning on agent output.
    Returns (possibly redacted) text.
    """
    sec = config.security
    try:
        from olano.security.input_scan import scan_output
        from olano.security.pii_middleware import apply as pii_apply

        # Credential leak scan
        result = scan_output(text)
        if not result.is_safe:
            logger.warning(
                "Security: output scan found potential credential leak [agent=%s]",
                config.name,
            )

        if getattr(sec, "credential_scrub", True):
            text, findings = scrub_text(
                text, patterns_path=getattr(sec, "scrub_patterns_path", None)
            )
            if findings:
                logger.warning(
                    "Security: credential scrub redacted %d finding(s) [agent=%s]",
                    len(findings),
                    config.name,
                )

        # PII scan on output — like input, exempt for peer (talk_to_agent)
        # replies; the calling agent's own human-facing output scan still runs.
        from olano.security.pii_middleware import peer_exchange_exempt

        if sec.pii_scan_output and not peer_exchange_exempt(sec):
            cleaned, findings = pii_apply(text, sec.pii_action)
            if findings:
                types = {f["type"] for f in findings}
                ctx = get_request_context()
                preview = _pii_redacted_preview_for_log(text, cleaned, sec.pii_action)
                logger.warning(
                    "Security: PII found in output [agent=%s types=%s action=%s "
                    "source=%s channel=%s user=%s session=%s redacted_preview=%s]",
                    config.name,
                    types,
                    sec.pii_action,
                    ctx.source,
                    ctx.channel,
                    ctx.user_id,
                    ctx.session_id or "-",
                    preview,
                )
            return cleaned
    except Exception as e:
        logger.debug("Security: output scan skipped: %s", e)
    return text


def _tool_error_reason(content: str) -> str:
    text = content.strip()
    for prefix in ("Error:", "[blocked]"):
        if text.startswith(prefix):
            text = text[len(prefix) :].strip()
    summarized = _summarize_tool_error_reason(text)
    if summarized:
        return summarized
    return text or "the requested action was blocked."


def _summarize_tool_error_reason(text: str) -> str | None:
    lowered = text.lower()

    if "temporary failure in name resolution" in lowered or (
        "failed to establish a new connection" in lowered and "/simple/" in lowered
    ):
        requirement = _extract_pip_requirement(text)
        if requirement and requirement.lower() == "yaml":
            return (
                "pip couldn't reach PyPI because the Docker sandbox has no working network access, "
                "and even with network access there is no package named 'yaml'; use 'PyYAML' instead."
            )
        return "pip couldn't reach PyPI because the Docker sandbox has no working network access."

    requirement = _extract_pip_requirement(text)
    if requirement:
        if requirement.lower() == "yaml":
            return "there is no package named 'yaml'; use 'PyYAML' instead."
        return f"pip couldn't find a distribution for '{requirement}'."

    return None


def _extract_pip_requirement(text: str) -> str | None:
    match = _PIP_REQUIREMENT_RE.search(text)
    if not match:
        return None
    return match.group(1).strip()


def _explicit_peer_target_from_message(text: str, self_name: str) -> str | None:
    """Return canonical peer name when the user explicitly says "ask <peer>"."""
    if not text:
        return None
    try:
        from olano.tools.talk_to import _resolve_peer_name
    except Exception:
        return None

    for match in _EXPLICIT_ASK_PEER_RE.finditer(text):
        candidate = match.group(1)
        canonical, _ = _resolve_peer_name(candidate)
        if canonical and canonical != self_name:
            return canonical
    return None


def _peer_tool_args_from_stream(args: Any) -> str | None:
    """Return a display-ready peer tool args JSON string once target/message exist."""
    if isinstance(args, dict):
        agent_name = str(args.get("agent_name") or args.get("agent") or "").strip()
        message = str(args.get("message") or args.get("content") or "").strip()
    else:
        text = str(args or "")
        agent_name = ""
        message = ""
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            agent_name = str(parsed.get("agent_name") or parsed.get("agent") or "").strip()
            message = str(parsed.get("message") or parsed.get("content") or "").strip()
        else:
            m_agent = re.search(r'"agent_name"\s*:\s*"([^"]+)"', text)
            m_msg = re.search(r'"message"\s*:\s*"([^"]*)"', text)
            agent_name = m_agent.group(1).strip() if m_agent else ""
            message = m_msg.group(1).strip() if m_msg else ""
    if not agent_name:
        return None
    return json.dumps({"agent_name": agent_name, "message": message})


# The JSONL row for a `task` call keeps the subagent's whole brief, bounded:
# the generic `content` column is cut at 1200 chars, which would leave the
# row's JSON unparseable and the reloaded card showing raw text.
_TASK_ROW_DESCRIPTION_LIMIT = 4000


def _task_card_args(raw: str) -> tuple[dict[str, str], bool] | None:
    """What a `task` card shows, read from the args streamed so far.

    Returns ``(args, complete)``. ``complete`` is True once the JSON has closed,
    so ``description`` is the model's whole brief. Before that it is a
    best-effort read of the partial text: the card can be announced as soon as
    ``subagent_type`` is known, but the model may stream ``subagent_type``
    first, in which case ``description`` is ``""`` here and only the complete
    read has it. ``None`` while even ``subagent_type`` is still arriving.
    """
    text = str(raw or "").strip()
    if text.endswith("}"):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            return (
                {
                    "subagent_type": str(parsed.get("subagent_type") or ""),
                    "description": str(parsed.get("description") or ""),
                },
                True,
            )
    m = re.search(r'"subagent_type"\s*:\s*"([^"]+)"', text)
    if not m:
        return None
    d = re.search(r'"description"\s*:\s*"([^"]*)"', text)
    return ({"subagent_type": m.group(1), "description": d.group(1) if d else ""}, False)


def _task_row_args(shown: dict[str, str]) -> str:
    """The structured ``args`` a `task` row persists: valid JSON, brief bounded."""
    return json.dumps(
        {
            "subagent_type": shown.get("subagent_type", ""),
            "description": _truncate_session_debug_text(
                shown.get("description", ""), _TASK_ROW_DESCRIPTION_LIMIT
            ),
        }
    )


# deepagents' PatchToolCallsMiddleware answers dangling tool calls (a turn stopped
# mid-step) by overwriting the whole message list, and the messages stream emits
# every message of that overwrite: the previous turn's text, tool calls and
# results arrive at the start of the next turn as if they were new. Nothing that
# node emits is output of the current turn.
_HISTORY_PATCH_NODE = "PatchToolCallsMiddleware.before_agent"


def _repeated_failure_response(tool_name: str, content: str, attempts: int) -> str:
    """Message emitted when the circuit breaker trips (same call failed repeatedly).

    Recoverable tool errors no longer abort the turn — the error flows back to the
    model so it can diagnose and try a different approach. The breaker only fires
    when the *identical* call keeps failing, to stop unproductive loops.
    """
    # The result quoted here may end in the middleware's "one more failure ends
    # the turn" notice; the turn has ended, so that line is no longer true.
    reason = _tool_error_reason(strip_repeat_failure_notice(content))
    if reason:
        reason = reason[0].lower() + reason[1:] if reason[0].isupper() else reason
    return (
        f"I tried that {attempts} times and it kept failing because {reason} "
        "I've stopped to avoid looping — tell me how you'd like to proceed, or "
        "what I'm missing to get this working."
    )


def _manual_action_indices(
    actions: list[dict[str, Any]],
    config: AgentConfig | None = None,
) -> list[int]:
    """Positions in ``actions`` that need a human/handler decision.

    Deliberately index-based: decisions are applied back onto ``actions`` by
    position, and position is the only reliable key — an action dict is
    unhashable, and its ``interrupt_id`` is shared by every action request of
    the same interrupt. Selecting the manual subset as indices means there is
    no re-matching step whose failure could drop a rejection.
    """
    return [
        idx for idx, action in enumerate(actions) if not _is_action_auto_approved(action, config)
    ]


def _is_action_auto_approved(action: dict[str, Any], config: AgentConfig | None = None) -> bool:
    """Return True when an interrupt action can be auto-approved safely."""
    name = action.get("name", "")
    if name in _ALWAYS_ALLOWED_PROFILE_TOOLS:
        return True
    if _always_asks_human(action):
        return False
    if name in _CROSS_AGENT_EXEC_TOOL_NAMES:
        # Cross-agent execution is approved one command at a time: remembered
        # "always" rules and the low-risk switch never bypass the human here.
        return False
    if config:
        try:
            from olano.security.hitl import is_tool_rule_allowed

            if is_tool_rule_allowed(config.name, name, "*"):
                return True
        except Exception:
            pass
    if not config or not getattr(config.security, "hitl_auto_approve_low_risk", False):
        return False
    return _classify_interrupt_action_risk(action) == "low"


def _classify_interrupt_action_risk(action: dict[str, Any]) -> str:
    """Return a low/medium/high risk tier for an interrupt action."""
    name = action.get("name", "")
    if name in _ALWAYS_ALLOWED_PROFILE_TOOLS:
        return "low"
    if _always_asks_human(action):
        return "high"
    if name in _CROSS_AGENT_EXEC_TOOL_NAMES:
        # Never "low": even a read-only command runs inside ANOTHER agent's
        # workspace, so the low-risk auto-approve switch must not skip the human.
        command_risk = classify_shell_command_risk(_extract_action_command(action))
        return "high" if command_risk == "high" else "medium"
    if name == "wallet_send" or name in _SECRET_TOOL_NAMES:
        return "high"
    if name in _WRITE_TOOL_NAMES or name == "task":
        return "medium"
    if name in _INTERRUPTIBLE_SHELL_TOOL_NAMES:
        return classify_shell_command_risk(_extract_action_command(action))
    return "medium"


def _extract_action_command(action: dict[str, Any]) -> str:
    """Extract a shell command string from an interrupt action payload."""
    args = action.get("args", {})
    if isinstance(args, dict):
        return str(args.get("command") or args.get("cmd") or "")
    if isinstance(args, str):
        raw = args.strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
            except Exception:
                return raw
            if isinstance(parsed, dict):
                return str(parsed.get("command") or parsed.get("cmd") or raw)
        return raw
    return ""


# LangGraph recognises a resume *map* by every key being an xxh3-128 hexdigest
# (`pregel/_utils.py:is_xxh3_128_hexdigest`); `Interrupt.id` is exactly that
# (`Interrupt.from_ns` → `xxh3_128_hexdigest(ns)`). Anything else — including our
# own `{"decisions": [...]}` — is read as one unscoped resume value.
_INTERRUPT_ID_RE = re.compile(r"[0-9a-f]{32}")


def _resume_payload_for_actions(
    actions: list[dict[str, Any]],
    decisions: list[dict[str, Any]],
) -> dict[str, Any]:
    """Shape the ``Command(resume=...)`` value for the pending interrupt(s).

    deepagents' HITL middleware reads ``interrupt(request)["decisions"]`` and
    raises unless it gets exactly one decision per action request **of that
    interrupt**, so the decisions have to be regrouped onto the interrupt that
    raised them — ``_extract_interrupt_actions`` flattens them across interrupts.

    One pending interrupt: the bare ``{"decisions": [...]}`` value is what the
    middleware wants and LangGraph applies it to the only parked task.

    More than one — the parent pauses on `task`, approves, and each subagent
    branch then parks on its own gated tool — LangGraph refuses a single
    unscoped value outright::

        RuntimeError: When there are multiple pending interrupts, you must
        specify the interrupt id when resuming.

    and wants a ``{interrupt_id: resume_value}`` map instead. Fall back to the
    flat shape if any id is missing or not a hexdigest: LangGraph would read a
    part-hexdigest dict as a scalar resume value anyway, and a malformed map
    reaches the middleware as a decision list it cannot parse.
    """
    grouped: dict[str, list[dict[str, Any]]] = {}
    for action, decision in zip(actions, decisions, strict=False):
        grouped.setdefault(str(action.get("interrupt_id", "") or ""), []).append(decision)

    if len(grouped) > 1:
        if all(_INTERRUPT_ID_RE.fullmatch(iid) for iid in grouped):
            return {iid: {"decisions": iid_decisions} for iid, iid_decisions in grouped.items()}
        # Real `Interrupt.id`s are always hexdigests, so this should be
        # unreachable — but it is the one remaining path that resumes several
        # interrupts with an unscoped value, i.e. the one that still raises.
        logger.warning(
            "resume: %d pending interrupts but ids are not resume-map keys (%s) — "
            "falling back to an unscoped resume, which LangGraph may refuse",
            len(grouped),
            sorted(grouped),
        )
    return {"decisions": list(decisions)}


async def _defer_to_inbox(
    action: dict[str, Any],
    config: AgentConfig | None,
    origin: dict[str, Any],
    risk: str,
) -> dict[str, Any]:
    """Route a gated action to the approvals inbox and await a human decision.

    Blocks the current turn on an ``asyncio`` future (resolved by the dashboard
    / a channel via ``hitl.resolve``) until approved/rejected or the agent's
    ``hitl_timeout_seconds`` elapses → reject. If no delivery channel is wired
    (e.g. a one-shot CLI run with no runtime inbox) we don't hang: a human-rooted
    chain approves (a human initiated it); anything else rejects.
    """
    from olano.security import hitl

    if not hitl.has_delivery():
        return {"type": "approve"} if origin.get("interactive") else {"type": "reject"}

    name = str(action.get("name", "tool"))
    timeout = getattr(getattr(config, "security", None), "hitl_timeout_seconds", 300) or 300
    try:
        approved = await hitl.request_approval(
            agent_name=getattr(config, "name", "") or "",
            action=name,
            description=f"{name} — requested by a {origin.get('root_source')} run",
            payload={
                "args": action.get("args"),
                "origin": origin,
                "interrupt_id": action.get("interrupt_id", ""),
                "risk": risk,
            },
            risk=risk,
            timeout_seconds=int(timeout),
        )
        return {"type": "approve"} if approved else {"type": "reject"}
    except Exception as exc:  # never let a delivery hiccup wedge the turn
        logger.warning("HITL defer failed for %s: %s", name, exc)
        return {"type": "reject"}


async def _resolve_autonomous_decisions(
    actions: list[dict[str, Any]],
    config: AgentConfig | None,
    origin: dict[str, Any],
) -> list[dict[str, Any]]:
    """Decide approve/reject/defer for interrupt actions when no interactive
    approval handler is reachable (the ``ainvoke`` path and ``astream_events``
    with no handler). Origin-aware (see ``request_context.current_origin``):

      - ``scheduled``  → approve (run freely).
      - ``cognition``  → approve iff the cognition auto-approve switch is on
                         AND the action falls outside the agent's
                         ``cognition.approval_required`` categories
                         (external_send / irreversible / identity_edit by
                         default — see ``cognition/approval_guard``);
                         anything held, or auto-approve off, defers to the
                         approvals inbox.
      - ``human``      → defer to the inbox (a delegated/nested A2A call rooted at
                         a human; the human resolves it). Direct human chat never
                         reaches here — it forwards via its own handler.
      - ``peer-unknown`` / headless / unknown → low-risk approve, else defer.
    """
    root = str(origin.get("root_source") or "")

    # Cross-agent command execution is carved out of every blanket approval: a
    # command that runs inside another agent's workspace is always deferred to a
    # human, even on a scheduled or auto-approved cognition turn. With no
    # delivery channel reachable, _defer_to_inbox fails closed (reject) for a
    # non-interactive origin — which is the safe outcome here.
    # Browser approvals are carved out the same way (_always_asks_human).
    def _is_cross_agent_exec(action: dict[str, Any]) -> bool:
        return action.get("name", "") in _CROSS_AGENT_EXEC_TOOL_NAMES or _always_asks_human(action)

    if root == "scheduled":
        return [
            await _defer_to_inbox(a, config, origin, _classify_interrupt_action_risk(a))
            if _is_cross_agent_exec(a)
            else {"type": "approve"}
            for a in actions
        ]

    if root == "cognition":
        approve_all = False
        required: frozenset[str] = frozenset()
        try:
            from olano.config.system_settings import (
                approval_required_categories,
                auto_approve_enabled,
            )

            approve_all = auto_approve_enabled(config)
            required = approval_required_categories(config)
        except Exception:
            approve_all = False
        if approve_all:
            from olano.cognition.approval_guard import action_categories, held_by

            # Fully Autonomous approves the run's tool calls — except the
            # categories the owner said must always see a human. A held action
            # defers exactly like an auto-approve-off turn would.
            def _held(a: dict[str, Any]) -> bool:
                return bool(
                    held_by(action_categories(str(a.get("name", "")), a.get("args")), required)
                )

            return [
                await _defer_to_inbox(a, config, origin, _classify_interrupt_action_risk(a))
                if _is_cross_agent_exec(a) or _held(a)
                else {"type": "approve"}
                for a in actions
            ]

    decisions: list[dict[str, Any]] = []
    for action in actions:
        risk = _classify_interrupt_action_risk(action)
        if root in ("human", "cognition"):
            decisions.append(await _defer_to_inbox(action, config, origin, risk))
        elif risk == "low":
            decisions.append({"type": "approve"})
        else:
            decisions.append(await _defer_to_inbox(action, config, origin, risk))
    return decisions


# The current turn's budget reservation id (set by _begin_deployment_turn,
# consumed by _record_usage — which runs in the turn's copied context via
# asyncio.to_thread). reconcile() is idempotent, so repeated consumption or a
# stale value from a previous turn is harmless.
_DEPLOYMENT_RESERVATION: ContextVar[str | None] = ContextVar(
    "deployment_budget_reservation", default=None
)

# The running turn's id (set by astream_events for the whole turn), stamped on
# its assistant rows so a reply can be named later: the dashboard's feedback
# votes key on it (core/feedback.py), and it is the turn_id the chat's `start`
# frame already gave the client.
_TURN_ID: ContextVar[str] = ContextVar("olano_turn_id", default="")


def _raise_deployment_denial(decision: Any) -> None:
    from olano.core.actionable_errors import REMEDY_NONE, ActionableError, Remediation

    raise AgentError(
        decision.message,
        detail=ActionableError(
            code=decision.code,
            message=decision.message,
            remediation=Remediation(kind=REMEDY_NONE, summary=decision.remediation),
        ),
    )


def _begin_deployment_turn(config: AgentConfig) -> None:
    """Per-turn deployment gates, checked at turn start so remote settings
    changes bite already-running agents:

    1. Quarantine: only the default agent may take turns (recovery surface).
    2. Usage budgets: atomically check every token/cost budget and book a
       provisional reservation (reconciled with actuals in _record_usage) so
       concurrent turns cannot jointly slip past a nearly-exhausted budget.

    Raises a structured :class:`AgentError` on denial. Open-posture installs
    short-circuit inside the helpers (no reservation, no writes).
    """
    try:
        from olano.deployment import enforcement as dep_enforcement
        from olano.deployment import usage_counters as dep_usage

        decision = dep_enforcement.agent_turn_allowed(config.name)
    except Exception:
        # The helpers never raise for policy outcomes; this guards import-time
        # breakage only. The resolver itself fails closed for managed
        # installs, so this is not a managed-mode bypass.
        logger.exception("deployment turn gate failed; allowing turn")
        return
    if not decision.allowed:
        _raise_deployment_denial(decision)
    reservation_id, budget_decision = dep_usage.try_reserve(config.name)
    _DEPLOYMENT_RESERVATION.set(reservation_id)
    if not budget_decision.allowed:
        _raise_deployment_denial(budget_decision)


def _record_usage(config: AgentConfig, usage_meta: dict[str, int], thread_id: str = "") -> float:
    """Record token usage against budget via CostTracker; return the call's USD cost."""
    if not usage_meta:
        return 0.0
    usd = 0.0
    try:
        from olano.config.workspace import get_workspace
        from olano.pulse.cost_tracker import CostTracker

        # A `default:<tier>` alias must never reach pricing or the ledger: the
        # cost tables and the olano-vs-BYOK fork below only know concrete
        # models, so an unresolved alias would meter as an unknown BYOK model
        # (cost 0, credits misattributed).
        billing_model = _resolved_model(config.model) or config.model

        ws = get_workspace(config.workspace)
        tracker = CostTracker(config.name, ws.state_dir, config.budget)
        input_tokens = usage_meta.get("input_tokens", 0)
        output_tokens = usage_meta.get("output_tokens", 0)
        cache_read = usage_meta.get("cache_read", 0)
        # Budget tracking stays gross (conservative); the displayed/ledger cost is
        # cache-discounted via the shared cost helper so it matches the drill-down.
        tracker.record_usage(
            model=billing_model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
        from olano.analytics.threadcost.cost import cost_for_call

        usd = cost_for_call(billing_model, input_tokens, output_tokens, cache_read)
        # Mirror the call into the cross-agent analytics ledger so the dashboard
        # can slice usage by agent + model + day. Best-effort — never blocks the turn.
        try:
            from olano.analytics.usage_ledger import record_usage_event

            # Olano-managed calls carry the gateway's own charge; BYOK calls
            # have none (their cost is the provider estimate above, in USD).
            credits_micro = usage_meta.get("credits_micro", 0)
            record_usage_event(
                agent=config.name,
                model=billing_model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                usd=usd,
                cache_read_tokens=cache_read,
                credits=(credits_micro / 1_000_000) if credits_micro else None,
                # A turn is one ledger row but many model calls; carrying the
                # count keeps the ledger's "model calls" agreeing with the
                # checkpoint-mined figure the Insights daily view shows.
                calls=int(usage_meta.get("model_calls", 0) or 0) or 1,
                # Runs in the turn's copied context (same guarantee the
                # _DEPLOYMENT_RESERVATION read relies on), so the request
                # context's root source is still bound here.
                origin=turn_origin_label(),
                # Which conversation spent it, so the drill-down can rank
                # threads from the ledger instead of from surviving checkpoint
                # state. "" when the caller has no thread (background writers).
                thread_id=thread_id,
            )
        except Exception:
            logger.debug("usage_ledger: mirror failed", exc_info=True)
        # Deployment budgets (durable counters): replace the turn's provisional
        # reservation with actuals, then hard-stop when a signed budget is
        # exhausted. Post-call this only fires for genuine exhaustion — an
        # accounting failure logs and defers to the pre-turn gate instead of
        # killing an already-delivered turn.
        dep_decision = None
        try:
            from olano.deployment import usage_counters as dep_usage
            from olano.models.olano_gateway import is_olano_model

            # The deployment cost budget exists to bound what OLANO pays
            # upstream — it is a runaway-spend guard on our own bill, not a
            # limit on the customer's provider account. So it counts
            # gateway-billed spend and nothing else.
            #
            # Both halves of that were wrong here. A BYOK call contributed its
            # provider-price estimate, throttling an agent over money that was
            # never ours; a gateway call contributed nothing, because
            # cost_for_call() deliberately returns 0.0 for olano:* (the
            # authoritative charge is the credits the gateway reports, and any
            # USD it invented would be fiction). The budget therefore measured
            # exactly the spend it was not meant to bound.
            #
            # Gateway charge in USD = credits / credits-per-USD, at the synced
            # published rate (bundled fallback; never zero, never raises).
            credits_micro_charge = int(usage_meta.get("credits_micro", 0) or 0)
            if credits_micro_charge:
                from olano.catalog.credits_registry import credits_per_usd

                micro_usd = round(credits_micro_charge / credits_per_usd())
            elif is_olano_model(billing_model):
                # A gateway call whose charge didn't come back: priced at zero
                # rather than at the provider estimate, which is not our bill.
                micro_usd = 0
            else:
                micro_usd = 0  # BYOK: the customer's provider, the customer's bill.
            reservation_id = _DEPLOYMENT_RESERVATION.get()
            if reservation_id:
                dep_usage.reconcile(
                    reservation_id,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_micro_usd=micro_usd,
                )
            else:
                dep_usage.record(
                    config.name,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    cost_micro_usd=micro_usd,
                )
            dep_decision = dep_usage.check_budget(config.name)
        except Exception:
            logger.exception("deployment budget accounting failed")
        if (
            dep_decision is not None
            and not dep_decision.allowed
            and dep_decision.code.startswith("deployment_budget_")
        ):
            _raise_deployment_denial(dep_decision)
        within_budget, budget_msg = tracker.check_budget()
        if budget_msg:
            level = "warning" if within_budget else "error"
            logger.log(
                logging.WARNING if level == "warning" else logging.ERROR,
                "Budget: %s [agent=%s cost_this_call=$%.6f]",
                budget_msg,
                config.name,
                usd,
            )
            if not within_budget and config.budget.hard_stop:
                raise AgentError(f"Budget exceeded: {budget_msg}")
            # Enqueue PULSE alert if nearing limit
            if budget_msg:
                try:
                    from olano.pulse.engine import PulseEngine

                    ws2 = get_workspace(config.workspace)
                    engine = PulseEngine(config, ws2.state_dir)
                    daily = tracker.daily_usd()
                    limit = config.budget.daily_usd or config.budget.monthly_usd or 0
                    if limit:
                        engine.generate_budget_alert(daily, limit)
                except Exception:
                    pass
    except AgentError:
        raise
    except Exception as e:
        logger.debug("CostTracker: skipped: %s", e)
    return usd


# ─────────────────────────────────── AgentRunner ─────────────────────────────


# The helper moved to ``olano.core.stream_isolation`` so tools (``image_describe``)
# and cognition workers (``delegate_task``) can reach it without importing the
# runner — every model call made inside the graph needs it, not just this one.
# Kept under the old name because ``ainvoke`` and the tests call it that.
_detached_runnable_config = detached_runnable_config
_egress_exempt = egress_exempt


def _trace_origin(ctx: Any) -> str:
    """The origin a traced turn is labelled with: ``interactive``,
    ``scheduled``, ``cognition``, ``peer`` or ``external`` (inbound MCP/A2A)."""
    if str(getattr(ctx, "source", "") or "") in ("mcp", "a2a"):
        return "external"
    try:
        from olano.analytics.usage_recorder import turn_origin_label

        return turn_origin_label() or "interactive"
    except Exception:
        return "interactive"


class AgentRunner:
    """
    Wraps a compiled deepagent graph with session management and JSONL logging.

    Sessions are identified by thread_id, allowing memory continuity across turns.
    Security pipeline (PII, injection scan) and CostTracker are called here.
    """

    def __init__(self, agent: Any, config: AgentConfig) -> None:
        self.agent = agent
        self.config = config
        self.workspace = get_workspace(config.workspace)
        self._hook_contexts: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._post_turn_pulse_tasks: dict[str, asyncio.Task[Any]] = {}
        # Approximate RSS (MB) added while building this agent's compiled graph,
        # recorded by RuntimeServer._boot_agent. A directional per-agent memory
        # signal for the diagnostics report; None until measured / off-Linux.
        self._boot_rss_mb: float | None = None
        # Transient agents built for a per-turn model override (dashboard model
        # picker), keyed by the override model string. They reuse the same
        # name-keyed checkpointer as ``self.agent`` (so thread history is
        # preserved) and are never substituted for it — leaving the shared agent
        # untouched keeps the many same-agent streams the dashboard runs in
        # parallel from clobbering each other. Cleared by ``set_agent`` whenever
        # the base agent is rebuilt (e.g. ``/model``, ``/mode``).
        self._override_agents: dict[str, Any] = {}
        # One-shot note injected into the next turn's input (e.g. "MCP server X
        # came online / went offline"). Set by a live MCP reload so a running
        # conversation learns its toolset changed; consumed and cleared on the
        # next turn. See ``set_mcp_notice`` / ``astream_events``.
        self._pending_mcp_notice: str | None = None
        # thread_id → the Langfuse trace id of its latest traced turn
        # (`_begin_turn_trace`), for the chat route's `done` frame.
        self._last_trace_ids: dict[str, str] = {}
        # Serializes the read-modify-write on <thread>.state.json's pending
        # context notes: record_context_note runs from arbitrary tasks (the
        # delivery hook, secretary-mode ingestion) while _consume_context_notes
        # drains at turn start — unserialized, a note appended between the
        # drain's read and its clearing write is silently lost, not deferred.
        self._context_notes_lock = threading.Lock()
        # Stable-section snapshot of the compiled graph's system prompt
        # (olano.core.prompt_state.PromptState). Set eagerly by the runtime
        # at boot/hot-swap so it always describes the graph actually running;
        # None ⇒ lazily computed from config+workspace on first use (CLI,
        # tests). Threads persist the fingerprint they have seen in
        # ``<thread>.state.json`` and get a one-shot update notice when this
        # snapshot moves — see ``_prompt_change_notice``.
        self._prompt_state: Any | None = None
        # Load workspace middleware hooks (populated by builder.build_agent)
        try:
            from olano.core.builder import get_middleware_hooks

            self._mw_hooks = get_middleware_hooks(config.name)
        except Exception:
            self._mw_hooks = None

    def _session_path(self, thread_id: str) -> Path:
        d = self.workspace.agent_session_dir(self.config.name)
        return d / f"{thread_id}.jsonl"

    def _thread_state_path(self, thread_id: str) -> Path:
        d = self.workspace.agent_session_dir(self.config.name)
        return d / f"{thread_id}.state.json"

    def _append_session(
        self,
        thread_id: str,
        role: str,
        content: str,
        *,
        extra: dict[str, Any] | None = None,
    ) -> None:
        path = self._session_path(thread_id)
        entry: dict[str, Any] = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "ts": datetime.now(timezone.utc).isoformat(),
            "role": role,
            "content": content,
        }
        # Provenance. This is the ONLY place a turn's surface is recorded: the
        # persisted thread id for a platform chat is the bare platform chat id
        # (runtime/server.py: `source.thread_id or source.chat_id`), so nothing
        # about the file name says "Telegram" and the rail used to label every
        # platform conversation "Dashboard". `channel_for_request` returns ""
        # (and we write nothing) whenever the live context knows no more than
        # the read-time derivation already does, so this only ever adds
        # information. `origin` is deliberately never stamped; see that function.
        # Both keys are optional and every reader uses `.get`, so
        # SESSION_SCHEMA_VERSION stays where it is.
        try:
            ctx = get_request_context()
            channel = channel_for_request(ctx)
            if channel:
                entry["channel"] = channel
            if role == "user":
                # The platform display name, already resolved by the adapter.
                # Recording it makes `sender` authoritative instead of recovered
                # by regex from the `[from …]` tag — which the PII scrubber can
                # rewrite out from under us. A bare id is exactly the noise
                # thread_labels exists to remove, so it never counts as a name.
                name = str((ctx.metadata or {}).get("name") or "").strip()
                if name and name != str(ctx.user_id or ""):
                    entry["sender"] = name
                # A prompt written for the model, not the human (the onboarding
                # greeting): persisted for cold-start rehydration, hidden by
                # every transcript reader (`api._transcript_message`). Only
                # the prompt row — the reply it produces is the point.
                if (ctx.metadata or {}).get(HIDDEN_PROMPT_KEY):
                    entry["hidden"] = True
            elif role == "assistant":
                turn_id = _TURN_ID.get()
                if turn_id:
                    entry["turn_id"] = turn_id
                # The Langfuse trace this reply belongs to, when the turn was
                # traced: the dashboard builds its "Open in Langfuse" link and
                # attaches feedback scores from it. Optional like the rest.
                from olano.observability.tracing import current_trace

                active = current_trace()
                if active is not None:
                    entry["trace_id"] = active.trace_id
        except Exception:  # pragma: no cover - provenance must never lose a turn
            logger.debug("session provenance stamp skipped", exc_info=True)
        # Caller-supplied keys (the assistant turn's ordered ``segments``, a
        # steer's ``sender``) land AFTER the provenance stamp so they can
        # override it — a mid-turn steer names the human who typed it, not
        # the one who started the turn. Optional and read with ``.get``
        # everywhere, so the schema version stays where it is.
        if extra:
            entry.update(extra)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self._publish_row(thread_id, entry)

    def append_session_entry(self, thread_id: str, entry: dict[str, Any]) -> None:
        """Append an arbitrary JSONL session entry."""
        path = self._session_path(thread_id)
        payload = dict(entry)
        payload.setdefault("schema_version", SESSION_SCHEMA_VERSION)
        payload.setdefault("ts", datetime.now(timezone.utc).isoformat())
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")
        self._publish_row(thread_id, payload)

    # ── live thread events ─────────────────────────────────────────────────
    # Every transcript append and every turn boundary is announced on the
    # dashboard's /api/stream (core/thread_events.py), from HERE and nowhere
    # else: the runner is the one code path every surface — dashboard chat,
    # Telegram, scheduler, the OpenAI connector, peers, webhooks — goes
    # through, so a chat open on another device sees the thread move without
    # each caller remembering to say so. No-ops outside the runtime server.

    def _publish_row(self, thread_id: str, entry: dict[str, Any]) -> None:
        try:
            from olano.core.thread_events import publish_thread_updated

            publish_thread_updated(self.config.name, thread_id, entry)
        except Exception:  # a broadcast must never cost a turn
            logger.debug("thread_updated publish skipped", exc_info=True)

    def _turn_channel(self) -> str:
        """The surface this turn arrived on, as `_append_session` would stamp it."""
        try:
            ctx = get_request_context()
            return channel_for_request(ctx)
        except Exception:
            return ""

    # ── mid-turn steering (core/steering.py) ───────────────────────────────

    def steer(
        self,
        thread_id: str,
        text: str,
        *,
        turn_id: str | None = None,
        steer_id: str | None = None,
        sender: str = "",
    ) -> Any:
        """Hand a message to the turn running on ``thread_id``.

        The model reads it at its next step (after the tool calls in flight
        finish, before its next model call) — `SteerMiddleware` appends it to
        the graph state; `astream_events` then reports it as a ``("steer", …)``
        event and a ``user`` transcript row stamped ``steer: true``.

        Returns the accepted ``SteerMessage``, or ``None`` when no turn is
        running there (or ``turn_id`` names a turn that already ended) — the
        caller sends the message as a normal turn instead. The text goes
        through the same PII/injection gate as any user message
        (``AgentError`` when blocked); ``ValueError`` for an empty/oversized
        text or a turn already holding the per-turn maximum.
        """
        from olano.core.steering import REGISTRY

        text = _scan_input(text, self.config)
        return REGISTRY.steer(
            self.config.name,
            thread_id,
            text,
            expected_turn_id=turn_id,
            steer_id=steer_id,
            sender=sender,
            channel=self._turn_channel(),
        )

    def cancel_steer(self, thread_id: str, steer_id: str) -> str:
        """Withdraw a steer the model has not read yet — see
        :meth:`olano.core.steering.SteerRegistry.cancel` for the outcomes."""
        from olano.core.steering import REGISTRY

        return REGISTRY.cancel(self.config.name, thread_id, steer_id)

    @staticmethod
    def _model_stamp(config: AgentConfig) -> dict[str, Any]:
        """The provenance keys every assistant row carries: which model answered.

        ``model`` is the ``provider:model`` string the turn actually ran under,
        spelled exactly as the caller chose it (a dashboard override, the
        configured model, a ``default:*`` alias), so a reader can hand it
        straight back to the model picker. ``reasoning_effort`` rides along
        only when one was set. This is the ONLY record of the model behind a
        reply: the checkpoint keeps the provider's bare model id and the usage
        ledger is not indexed by thread, so without it a reopened conversation
        could not tell which model it had been talking to. Both keys are
        optional and read with ``.get``, so ``SESSION_SCHEMA_VERSION`` stays
        where it is.
        """
        stamp: dict[str, Any] = {"model": str(config.model or "")}
        effort = getattr(config, "reasoning_effort", None)
        if effort:
            stamp["reasoning_effort"] = str(effort)
        return stamp

    async def _append_assistant_text(
        self,
        thread_id: str,
        text: str,
        hook_context: dict[str, Any],
        *,
        extra: dict[str, Any] | None = None,
        config: AgentConfig | None = None,
    ) -> str:
        """Scan + run output hooks on a reply segment, then persist it as an
        ``assistant`` row. One turn writes one row — or several, when a steer
        split the reply (see ``_flush_steers``): each segment gets the same
        treatment the whole reply used to.

        ``extra`` may carry the turn's ordered ``segments``. Each one is scanned
        like the full text so a redaction can never leak through the structured
        copy; an output hook that rewrote the reply makes them stale, so they
        are dropped and the row falls back to its single ``content``.

        ``config`` is the config the turn actually ran under (a per-turn model
        override builds a transient copy); it is what the row's model stamp
        records. Defaults to the runner's own.
        """
        extra = {**self._model_stamp(config or self.config), **(extra or {})}
        scanned = _scan_output(text, self.config)
        full = scanned
        if self._mw_hooks and not self._mw_hooks.is_empty():
            try:
                from olano.core.middleware_loader import run_output_hooks

                full = await run_output_hooks(full, self._mw_hooks, self.config, hook_context)
            except Exception:
                pass
        if extra and "segments" in extra:
            if full != scanned:
                extra = {k: v for k, v in extra.items() if k != "segments"} or None
            else:
                extra = {
                    **extra,
                    "segments": [
                        {
                            "text": _scan_output(str(seg["text"]), self.config),
                            "after": seg.get("after"),
                        }
                        for seg in extra["segments"]
                    ],
                }
        # A reply to a peer is attributed to that exchange (`via_peer`), the
        # row shape the Agent to Agent view and `talk_to` already read.
        ctx = get_request_context()
        if ctx.source == "peer" and ctx.user_id:
            extra = {**(extra or {}), "via_peer": ctx.user_id}
        self._append_session(thread_id, "assistant", full, extra=extra)
        return full

    def _publish_activity(self, thread_id: str, status: str) -> None:
        try:
            from olano.core.thread_events import publish_thread_activity

            publish_thread_activity(
                self.config.name, thread_id, status, channel=self._turn_channel()
            )
        except Exception:
            logger.debug("thread_activity publish skipped", exc_info=True)

    def load_thread_state(self, thread_id: str) -> dict[str, Any]:
        path = self._thread_state_path(thread_id)
        if not path.exists():
            return {}
        try:
            return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            return {}

    def save_thread_state(self, thread_id: str, state: dict[str, Any]) -> None:
        path = self._thread_state_path(thread_id)
        payload = dict(state)
        payload.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
        path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def update_thread_state(self, thread_id: str, **patch: Any) -> dict[str, Any]:
        state = self.load_thread_state(thread_id)
        state.update(patch)
        self.save_thread_state(thread_id, state)
        return state

    def _write_session_entries(self, thread_id: str, entries: list[dict[str, str]]) -> None:
        path = self._session_path(thread_id)
        with path.open("w", encoding="utf-8") as f:
            for entry in entries:
                payload: dict[str, Any] = dict(entry)
                payload.setdefault("schema_version", SESSION_SCHEMA_VERSION)
                f.write(json.dumps(payload) + "\n")

    def _append_tool_session(
        self,
        thread_id: str,
        role: str,
        *,
        name: str,
        content: str,
        args: str | None = None,
    ) -> None:
        # Keep the human-readable "name: content" string (used by transcripts and
        # the thread view) but ALSO persist the tool name as a structured field so
        # analytics (analytics/aggregate.tool_rollup) can attribute calls to the
        # real tool instead of falling back to the generic "tool".
        entry: dict[str, Any] = {
            "role": role,
            "name": name or "tool",
            "content": f"{name}: {_truncate_session_debug_text(content)}",
        }
        if args is not None:
            # Structured, parseable args the chat prefers over the "name: …"
            # string on reload (sessionStore.parseSessionTool), so a card that
            # parses its args (`task`) draws the same after a reload as live.
            entry["args"] = args
        self.append_session_entry(thread_id, entry)

    def _hook_context(self, thread_id: str) -> dict[str, Any]:
        # Bounded LRU: one entry per distinct thread_id would otherwise grow
        # forever (every dashboard session, scheduled_* / heartbeat_* / cog::*
        # thread). The value is deterministic from thread_id + static agent
        # fields, so evicting the least-recently-used entry is free — a returning
        # thread just rebuilds the identical dict.
        ctx = self._hook_contexts.get(thread_id)
        if ctx is None:
            ctx = {
                "thread_id": thread_id,
                "agent_name": self.config.name,
                "workspace_root": str(self.workspace.root),
            }
            self._hook_contexts[thread_id] = ctx
            while len(self._hook_contexts) > _HOOK_CONTEXTS_MAX:
                self._hook_contexts.popitem(last=False)
        else:
            self._hook_contexts.move_to_end(thread_id)
        return ctx

    def _request_context_metadata(
        self, thread_id: str, state: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "workspace_root": str(self.workspace.root),
            # Workspace-mutating tools that act outside the deepagents
            # permission layer (trash_*) honor this to keep a read-only
            # backend actually read-only. Only FilesystemBackendConfig
            # carries access_mode; every other backend is read/write.
            "workspace_access_mode": getattr(self.config.backend, "access_mode", "read_write"),
        }
        # Turn entry points that already loaded the thread state pass it in so
        # each turn reads <thread>.state.json exactly once.
        if state is None:
            state = self.load_thread_state(thread_id)
        session_shared_paths = state.get("session_shared_paths", [])
        if session_shared_paths:
            metadata["session_shared_paths"] = session_shared_paths
        return metadata

    def _invoke_config(self, thread_id: str) -> dict[str, Any]:
        # Bind the checkpoint key to THIS agent so two agents that the same human
        # talks to (same human-derived thread_id, e.g. a Telegram chat id) never
        # resume each other's LangGraph state. This is the single runtime chokepoint
        # — every ainvoke/astream/aget_state/aupdate_state/fork/compact routes here,
        # so reads and writes always agree on the same scoped key. The human-facing
        # thread_id (JSONL filenames, dashboard, session state) is untouched.
        return {"configurable": {"thread_id": scope_thread(self.config.name, thread_id)}}

    def _run_config(self, thread_id: str) -> dict[str, Any]:
        """:meth:`_invoke_config` for a graph *run* inside a turn: adds the
        turn's Langfuse callback handler and trace metadata when the turn is
        traced (``olano.observability``), and nothing otherwise. State reads
        and writes keep using :meth:`_invoke_config`, so they never show up as
        spans."""
        config = self._invoke_config(thread_id)
        from olano.observability.tracing import run_config_additions

        config.update(run_config_additions())
        return config

    async def _persist_breaker_stop(
        self,
        agent: Any,
        thread_id: str,
        graph_stream: Any,
        tool_messages: list[Any],
        stop_text: str,
    ) -> None:
        """Commit the step the circuit breaker stopped on, plus its stop message.

        The breaker stops reading the graph stream once its step's results are
        in, which is before LangGraph commits that step (it does so between
        steps, after the last parallel task returns), so the results the user
        just saw never reached the checkpoint. On the next turn deepagents'
        ``PatchToolCallsMiddleware`` filled the gap with "was cancelled -
        another message came in" (0.7.23: "did not complete - no result was
        recorded"), and the model, told its calls were
        interrupted rather than refused, repeated them. Writing the results
        back (``as_node="model"``, so nothing is left queued) keeps the real
        errors and the reason the turn stopped in the model's context. The
        results carry message ids, so a step LangGraph did manage to commit is
        replaced, not duplicated. A call whose result never arrived (the stream
        ended first) stays unanswered and is patched as cancelled, which for it
        is true.
        """
        try:
            await graph_stream.aclose()
        except Exception:
            logger.debug("Breaker stop: closing the graph stream failed", exc_info=True)
        messages: list[Any] = [*tool_messages]
        if stop_text:
            messages.append(AIMessage(content=stop_text))
        if not messages:
            return
        try:
            await agent.aupdate_state(
                self._invoke_config(thread_id), {"messages": messages}, as_node="model"
            )
        except Exception:
            logger.warning(
                "Breaker stop: could not record the failed step on thread %s; the next "
                "turn will see those calls as cancelled",
                thread_id,
                exc_info=True,
            )

    def set_agent(
        self,
        agent: Any,
        config: AgentConfig | None = None,
        prompt_state: Any | None = None,
    ) -> None:
        """Replace the compiled agent (e.g. after a live ``/model`` switch or a
        prompt-refresh hot-swap).

        ``config``, when given, is assigned together with the graph so a turn
        racing the swap never pairs the new graph with the stale config.
        ``prompt_state`` is the stable-section snapshot of the NEW graph
        (:class:`olano.core.prompt_state.PromptState`); omitted ⇒ the cached
        snapshot is invalidated and lazily recomputed on next use. Clears the
        per-turn model-override cache too, since a rebuilt base agent may carry
        new tools/prompt/config that the cached transient agents predate.
        """
        self.agent = agent
        if config is not None:
            self.config = config
        self._prompt_state = prompt_state
        self._override_agents.clear()

    def set_mcp_notice(self, text: str | None) -> None:
        """Queue a one-shot system note for the next turn (MCP online/offline)."""
        self._pending_mcp_notice = text or None

    def prompt_state(self) -> Any | None:
        """The stable-section snapshot of the running graph's system prompt.

        Set eagerly by the runtime right after each graph build (boot /
        hot-swap) so it describes the graph actually running. Falls back to a
        lazy compute from config+workspace for directly-constructed runners
        (CLI/tests, where the graph was just built from the same inputs).
        ``None`` / empty fingerprint ⇒ change detection is inert for this
        runner.
        """
        if self._prompt_state is None:
            try:
                from olano.core.prompt_state import compute_prompt_state

                self._prompt_state = compute_prompt_state(self.config, self.workspace)
            except Exception:  # pragma: no cover - defensive
                return None
        return self._prompt_state

    def refresh_prompt_state(self) -> Any | None:
        """Recompute the stable-section snapshot from disk.

        Blocking file IO — event-loop callers wrap this in
        ``asyncio.to_thread`` (see ``RuntimeServer._boot_agent_locked``).
        """
        try:
            from olano.core.prompt_state import compute_prompt_state

            self._prompt_state = compute_prompt_state(self.config, self.workspace)
        except Exception:  # pragma: no cover - defensive
            self._prompt_state = None
        return self._prompt_state

    def _prompt_change_notice(self, thread_id: str, state: dict[str, Any]) -> str | None:
        """One-shot in-thread notice when the stable prompt sections changed.

        Compares the thread's persisted ``prompt_sections_seen`` marker
        against this runner's current :meth:`prompt_state` and returns the
        rendered update notice when sections moved. Gating:

        - No marker AND no thread state AND no session JSONL yet → brand-new
          thread: persist nothing (fresh scheduled_/heartbeat_ runs would
          otherwise leave one ``.state.json`` per firing, forever).
        - No marker but the thread already exists → first sight
          (rollout / second turn): adopt the current fingerprint silently,
          mirroring SkillAwarenessMiddleware's baseline behavior.
        - Marker present and moved → persist the update; return the notice
          (``None`` when ``prompt_refresh.notify_threads`` is off — the
          marker still advances so a later opt-in doesn't replay old diffs).

        Never raises — a failure here must never break the turn.
        """
        try:
            current = self.prompt_state()
            if current is None or not getattr(current, "fingerprint", None):
                return None
            seen = state.get("prompt_sections_seen")
            if not isinstance(seen, dict) or not seen:
                if state or self._session_path(thread_id).exists():
                    self.update_thread_state(
                        thread_id, prompt_sections_seen=dict(current.fingerprint)
                    )
                return None

            from olano.core.prompt_state import diff_seen_map, render_prompt_update_notice

            changed, removed, updated_seen = diff_seen_map(seen, current)
            if updated_seen == seen:
                return None
            self.update_thread_state(thread_id, prompt_sections_seen=updated_seen)
            if not changed and not removed:
                return None

            from olano.config.system_settings import load_prompt_refresh_settings

            if not load_prompt_refresh_settings().notify_threads:
                return None
            notice = render_prompt_update_notice(changed, removed)
            if not notice:
                return None
            # Audit trail: the dashboard thread view filters system rows, but
            # cold-start rehydration replays them (_session_entries_to_messages).
            self.append_session_entry(thread_id, {"role": "system", "content": notice})
            logger.info(
                "prompt update notice for %s thread %s (sections: %s)",
                self.config.name,
                thread_id,
                ", ".join(sorted(set(changed) | set(removed))),
            )
            return notice
        except Exception:
            logger.debug("prompt-change notice failed for thread %s", thread_id, exc_info=True)
            return None

    def record_turn_error(
        self,
        thread_id: str,
        message: str,
        *,
        code: str = "",
        stage: str = "turn",
    ) -> None:
        """Tell the agent about an error its user was just shown.

        An error frame is drawn by the client and nothing else: the assistant's
        JSONL row is written when the turn ends, the frame is appended to the
        bubble in the browser, and the model's checkpoint never learns that
        anything went wrong. So the next turn starts with the user looking at a
        message the agent cannot see — and "what is this error?" gets answered
        by a model reading the words alone. That is exactly how one runtime bug
        ("Could not determine home directory.", raised while scanning a finished
        reply for file paths) was diagnosed back to its user as an unset ``HOME``
        on their machine, complete with remedies for a problem nobody had.

        The note carries the **classified, already-user-facing** message — never
        a traceback, a module path or an exception type — because it becomes
        context the agent may quote on any channel. ``stage`` says what the user
        actually received, which is the part the agent cannot infer and the part
        that decides what it should say:

        - ``after_reply`` — the answer was delivered in full and something
          downstream of it failed. Nothing to redo; the agent should not
          apologize for the work or offer to run it again.
        - ``partial`` — output had started when the turn broke, so the visible
          reply is cut off.
        - ``turn`` — the turn produced nothing.

        Never raises: it rides ``record_context_note``, whose queue is drained
        once into the next turn and capped, so a run of failures cannot grow
        without bound.
        """
        text = " ".join(str(message or "").split())[:400]
        if not text:
            return
        where = {
            "after_reply": (
                "Your reply was delivered in full, and then the runtime hit an error "
                "which the user can see below your answer"
            ),
            "partial": "Your reply was cut off partway through by an error the user can see",
            "turn": "Your previous turn failed with an error the user can see",
        }.get(stage, "The user was shown an error")
        suffix = f" (code: {code})" if code and code != "generic_error" else ""
        note = (
            f'{where}: "{text}"{suffix}. '
            "It was reported by the platform, not written by you — you have no other record of it. "
            "If the user asks what it means, answer from this note and say plainly when the cause "
            "is not something they or you did; do not guess at causes on their machine."
        )
        self.record_context_note(thread_id, note)

    def record_context_note(
        self,
        base_thread_id: str,
        note: str,
        *,
        skip_thread_id: str | None = None,
    ) -> str | None:
        """Fold a context-only event into a conversation without running the agent.

        Two callers, one contract: the owner speaking for themselves in a
        Telegram Business ("secretary") chat, and proactive outbound deliveries
        (notification tools, PULSE, scheduled digests) landing in a chat the
        agent is not currently serving. Both are messages the agent must *know
        about* on its next turn but must never respond to.

        ``base_thread_id`` is the chat-derived id the inbound handlers use
        (``source.thread_id or source.chat_id``); it resolves through the
        chat's persisted ``active_thread`` (set by ``/new``) exactly like a
        real message would, so the note lands in the thread the next reply
        will run on. The note is appended to that thread's JSONL transcript as
        a ``system`` row (audit trail; the dashboard filters these, cold-start
        rehydration replays them) and queued in ``<thread>.state.json`` for a
        one-shot system-message injection at the start of the next turn.

        ``skip_thread_id`` short-circuits recording when the resolved thread is
        the one currently executing — a notification sent back into the very
        conversation the agent is replying in already has the tool call and its
        result in history, so a note would only duplicate it.

        Returns the thread id the note was recorded under, or ``None`` when
        nothing was recorded. Never raises.
        """
        base = str(base_thread_id or "").strip()
        note_text = str(note or "").strip()
        if not base or not note_text:
            return None
        try:
            # Same key get_session_settings resolves (platforms.inbound); read
            # directly so this stays import-light and usable from any caller.
            from olano.platforms.inbound import ACTIVE_THREAD_KEY

            with self._context_notes_lock:
                base_state = self.load_thread_state(base)
                thread_id = str(base_state.get(ACTIVE_THREAD_KEY) or base)
                if skip_thread_id and thread_id == str(skip_thread_id):
                    return None
                self.append_session_entry(thread_id, {"role": "system", "content": note_text})
                state = base_state if thread_id == base else self.load_thread_state(thread_id)
                notes = [
                    n
                    for n in (state.get(PENDING_CONTEXT_NOTES_KEY) or [])
                    if isinstance(n, dict) and str(n.get("text") or "").strip()
                ]
                notes.append(
                    {
                        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "text": note_text,
                    }
                )
                if len(notes) > _CONTEXT_NOTES_MAX:
                    notes = notes[-_CONTEXT_NOTES_MAX:]
                self.update_thread_state(thread_id, **{PENDING_CONTEXT_NOTES_KEY: notes})
                return thread_id
        except Exception:
            logger.debug("record_context_note failed for thread %s", base_thread_id, exc_info=True)
            return None

    def _consume_context_notes(self, thread_id: str, thread_state: dict[str, Any]) -> str | None:
        """Drain queued context notes for one-shot injection into this turn.

        ``thread_state`` is the turn's already-loaded ``<thread>.state.json``,
        used as a cheap fast-path — the common turn has no notes and pays no
        extra I/O. When notes exist the file is re-read and cleared, so a note
        recorded between the turn's initial load and this drain still survives
        into the batch (or, at worst, the next turn). Never raises.
        """
        if not thread_state.get(PENDING_CONTEXT_NOTES_KEY):
            return None
        try:
            with self._context_notes_lock:
                current = self.load_thread_state(thread_id)
                notes = [
                    n
                    for n in (current.get(PENDING_CONTEXT_NOTES_KEY) or [])
                    if isinstance(n, dict) and str(n.get("text") or "").strip()
                ]
                self.update_thread_state(thread_id, **{PENDING_CONTEXT_NOTES_KEY: []})
            if not notes:
                return None
            lines = [
                f"- [{n['ts']}] {n['text']}" if n.get("ts") else f"- {n['text']}" for n in notes
            ]
            return _CONTEXT_NOTES_HEADER + "\n" + "\n".join(lines)
        except Exception:
            logger.debug("context-note drain failed for thread %s", thread_id, exc_info=True)
            return None

    async def _agent_for_model(
        self,
        override_model: str | None,
        override_reasoning_effort: str | None = None,
    ) -> tuple[Any, AgentConfig]:
        """Resolve the (agent, config) to run this turn against.

        No override (or overrides equal to the configured values) → the shared
        ``self.agent`` / ``self.config``, untouched. A real override → a
        transient agent bound to that model and/or reasoning effort, built once
        and cached per (model, effort) pair. The transient agent reuses the
        same name-keyed checkpointer, so thread history carries over; the
        shared agent/config are never mutated. Raises ``AgentError`` if the
        override model can't be built (surfaced to the caller as a clean error
        frame).
        """
        # Compare resolved forms so an override that is merely another spelling
        # of the configured brain (`default:turbo` vs the `olano:turbo` it
        # resolves to, or vice versa) reuses the shared agent instead of
        # building a redundant transient one.
        if override_model == self.config.model or (
            override_model and _resolved_model(override_model) == _resolved_model(self.config.model)
        ):
            override_model = None
        if override_reasoning_effort == self.config.reasoning_effort:
            override_reasoning_effort = None
        if not override_model and not override_reasoning_effort:
            return self.agent, self.config
        update: dict[str, Any] = {}
        if override_model:
            update["model"] = override_model
        if override_reasoning_effort:
            update["reasoning_effort"] = override_reasoning_effort
        cfg = self.config.model_copy(update=update)
        cache_key = f"{override_model or ''}|{override_reasoning_effort or ''}"
        agent = self._override_agents.get(cache_key)
        if agent is None:
            from olano.core.builder import build_agent_async, build_agent_with_mcp

            try:
                agent = await (
                    build_agent_with_mcp(cfg) if cfg.mcp_servers else build_agent_async(cfg)
                )
            except Exception as exc:
                raise AgentError(
                    f"Could not load model '{override_model or cfg.model}': {exc}",
                    original=exc,
                ) from exc
            self._override_agents[cache_key] = agent
        return agent, cfg

    @staticmethod
    def _extract_finish_reason_from_message(msg: Any) -> str | None:
        for attr in ("response_metadata", "additional_kwargs"):
            meta = getattr(msg, attr, None)
            if isinstance(meta, dict):
                reason = meta.get("finish_reason") or meta.get("stop_reason")
                if reason:
                    return str(reason)
        return None

    @classmethod
    def _extract_finish_reason(cls, result: Any) -> str | None:
        if isinstance(result, dict):
            direct = result.get("finish_reason") or result.get("stop_reason")
            if direct:
                return str(direct)
            for msg in reversed(result.get("messages", [])):
                reason = cls._extract_finish_reason_from_message(msg)
                if reason:
                    return reason
        return None

    # Usage accounting logic lives in olano/analytics/usage_recorder so
    # background call sites (cognition workers, highlights, PULSE, ACP) can
    # record spend without importing this module. These thin wrappers keep the
    # runner's internal call sites and existing tests working unchanged.
    @staticmethod
    def _merge_usage_metadata(target: dict[str, int], usage: Any) -> None:
        merge_usage_metadata(target, usage)

    @staticmethod
    def _merge_olano_credits(target: dict[str, int], msg: Any, *, managed: bool = True) -> None:
        merge_olano_credits(target, msg, managed=managed)

    @staticmethod
    def _ensure_streamed_credits(target: dict[str, int], model: str | None) -> None:
        # Resolve `default:*` aliases so the olano-vs-BYOK fork inside sees the
        # concrete model (an alias would always read as BYOK = no credits).
        ensure_streamed_credits(target, _resolved_model(model))

    @classmethod
    def _merge_result_usage(
        cls,
        target: dict[str, int],
        result: Any,
        *,
        model: str | None = None,
        seen_ids: set[str] | None = None,
    ) -> None:
        # Same alias resolution as _ensure_streamed_credits: the managed/BYOK
        # split inside merge_result_usage keys on the concrete model.
        merge_result_usage(target, result, model=_resolved_model(model), seen_ids=seen_ids)

    async def _billed_message_ids(self, thread_id: str, agent: Any | None = None) -> set[str]:
        """Ids of the messages already on this thread, as a ``seen_ids`` ledger.

        A checkpointed ``ainvoke`` returns the whole accumulated state, so
        anything that folds that result into a turn's usage must first know
        what predates the turn — otherwise every earlier turn is billed again
        (see ``merge_result_usage``). Failure is deliberately quiet and
        returns an EMPTY set, which bills the result as it always was: a
        checkpointer hiccup must not silently zero out a turn's cost, and a
        graph with no checkpointer returns only the turn's own messages
        anyway, so counting them all is right there.

        ``agent`` defaults to this runner's own graph; pass the one the turn is
        actually invoking (a ``/model`` switch runs a rebuilt graph) so the
        snapshot and the result come from the same place.
        """
        try:
            state = await (agent or self.agent).aget_state(self._invoke_config(thread_id))
            messages = (getattr(state, "values", None) or {}).get("messages", [])
            return {m.id for m in messages if getattr(m, "id", None)}
        except Exception:
            logger.debug("usage: prior thread state unreadable", exc_info=True)
            return set()

    async def _continue_truncated_response(
        self,
        thread_id: str,
        finish_reason: str | None,
        usage_meta: dict[str, int] | None = None,
        agent: Any | None = None,
        model: str | None = None,
    ) -> list[str]:
        if finish_reason != "length":
            return []

        # An explicit per-reply ceiling makes `finish_reason: "length"` the
        # EXPECTED end of a capped reply, not an accident to recover from.
        # Continuing here would re-invoke the whole graph up to
        # `truncation_retry_limit` times per turn and hand the sender the long
        # answer the cap exists to prevent — i.e. the feature would raise cost
        # instead of lowering it. The reply stops where the operator said it
        # stops; `message_policy.max_output_tokens` documents that.
        if getattr(getattr(self.config, "message_policy", None), "max_output_tokens", None):
            logger.debug(
                "Not continuing a length-truncated reply: message_policy."
                "max_output_tokens is set, so the cut is the configured cap."
            )
            return []

        agent = agent or self.agent
        segments: list[str] = []
        retry_limit = max(0, int(getattr(self.config, "truncation_retry_limit", 3) or 0))
        # Every continuation returns the accumulated thread, including the turn
        # that was already billed by the caller and every continuation before
        # it — one ledger across the loop bills each message once.
        seen_ids = (
            await self._billed_message_ids(thread_id, agent) if usage_meta is not None else None
        )
        for _ in range(retry_limit):
            result = await agent.ainvoke(
                {"messages": [{"role": "user", "content": _TRUNCATION_CONTINUE_PROMPT}]},
                config=self._run_config(thread_id),
            )
            if usage_meta is not None:
                self._merge_result_usage(
                    usage_meta, result, model=model or self.config.model, seen_ids=seen_ids
                )
            text = self._extract_response(result)
            if text:
                segments.append(text)
            finish_reason = self._extract_finish_reason(result)
            if finish_reason != "length":
                break
        return segments

    async def _generate_post_turn_pulse_items(
        self,
        thread_id: str,
        *,
        usage_meta: dict[str, int] | None = None,
        tool_failures: list[str] | None = None,
    ) -> None:
        try:
            from olano.pulse.generators import PulseGenerator

            generator = PulseGenerator(self.config, self.workspace.root)
            generator.capture_runtime_signals(
                usage_meta=usage_meta,
                tool_failures=tool_failures,
            )
            await generator.capture_agent_curated_pulse(thread_id=thread_id)
        except Exception as exc:
            logger.debug("Post-turn PULSE generation skipped for %s: %s", thread_id, exc)

    def _notify_revisions(self) -> None:
        """Turn-end ping to the home revision tracker (olano/revisions).

        A cheap flag-set on the debounced worker so files this turn touched
        get committed under this agent's authorship. No-op (and no import
        weight) outside the runtime server, and never raises.
        """
        try:
            from olano.revisions.worker import notify_turn

            notify_turn(self.config.name)
        except Exception:
            pass

    def _schedule_post_turn_pulse_generation(
        self,
        thread_id: str,
        *,
        usage_meta: dict[str, int] | None = None,
        tool_failures: list[str] | None = None,
    ) -> None:
        existing = self._post_turn_pulse_tasks.get(thread_id)
        if existing and not existing.done():
            return

        async def _run() -> None:
            try:
                await self._generate_post_turn_pulse_items(
                    thread_id,
                    usage_meta=usage_meta,
                    tool_failures=tool_failures,
                )
            finally:
                current = self._post_turn_pulse_tasks.get(thread_id)
                if current is task:
                    self._post_turn_pulse_tasks.pop(thread_id, None)

        task = asyncio.create_task(_run())
        self._post_turn_pulse_tasks[thread_id] = task

    def _bump_thread_credits(self, thread_id: str, usage_meta: dict[str, int]) -> None:
        """Accumulate this turn's Olano-gateway charge into the thread state.

        Per-thread credits exist nowhere else: the checkpoint store carries no
        gateway charge to mine and the usage ledger has no thread key, so
        without this a reopened thread's rail/header could never show what it
        cost in credits. Same inline-tiny-JSON posture as ``_append_session``;
        BYOK turns (credits_micro == 0) skip the write entirely.
        """
        credits_micro = int(usage_meta.get("credits_micro", 0) or 0)
        if credits_micro <= 0:
            return
        try:
            state = self.load_thread_state(thread_id)
            prev = float(state.get("credits_total") or 0.0)
            self.update_thread_state(
                thread_id, credits_total=round(prev + credits_micro / 1_000_000, 6)
            )
        except Exception:
            logger.debug("thread credits bump failed", exc_info=True)

    def _schedule_usage_recording(
        self,
        config: AgentConfig,
        usage_meta: dict[str, int],
        thread_id: str = "",
    ) -> None:
        """Record token usage off the turn's critical path.

        ``_record_usage`` does several synchronous file operations (cost tracker
        state, the cross-agent usage ledger, budget bookkeeping). Running it
        inline made the prompt wait on disk I/O after the last token; offloading
        it to a thread keeps that work out of the event loop. The budget
        hard-stop can no longer abort *this* turn — but the turn has already
        streamed its full answer by the time we get here, so the stop only ever
        applied retroactively. The warning is still logged from ``_record_usage``.
        """

        async def _run() -> None:
            try:
                await asyncio.to_thread(_record_usage, config, usage_meta, thread_id)
            except Exception:
                logger.debug("post-turn usage recording failed", exc_info=True)

        try:
            asyncio.create_task(_run())
        except RuntimeError:
            # No running loop (e.g. a sync caller) — record inline as a fallback.
            try:
                _record_usage(config, usage_meta, thread_id)
            except Exception:
                logger.debug("inline usage recording failed", exc_info=True)

    def _maybe_budget_notice(
        self,
        iteration_count: int,
        emitted: set[int],
    ) -> list[str]:
        budget = int(getattr(self.config, "iteration_budget", 0) or 0)
        if budget <= 0:
            return []

        notices: list[str] = []
        for threshold, text in (
            (max(1, (budget * 70 + 99) // 100), _BUDGET_NOTICE_70),
            (max(1, (budget * 90 + 99) // 100), _BUDGET_NOTICE_90),
        ):
            if iteration_count >= threshold and threshold not in emitted:
                emitted.add(threshold)
                notices.append(text)
        return notices

    # ----------------------------------------------------------------- sync

    def invoke(self, message: str, thread_id: str | None = None) -> str:
        thread_id = thread_id or str(uuid.uuid4())
        with push_request_context(
            session_id=thread_id,
            metadata=self._request_context_metadata(thread_id),
        ):
            message = _scan_input(message, self.config)
            self._append_session(thread_id, "user", message)

            result = self.agent.invoke(
                {"messages": [{"role": "user", "content": message}]},
                config=self._invoke_config(thread_id),
            )

            response = self._extract_response(result)
            response = _scan_output(response, self.config)
            self._append_session(
                thread_id, "assistant", response, extra=self._model_stamp(self.config)
            )
            return response

    def stream(self, message: str, thread_id: str | None = None) -> Iterator[str]:
        thread_id = thread_id or str(uuid.uuid4())
        with push_request_context(
            session_id=thread_id,
            metadata=self._request_context_metadata(thread_id),
        ):
            message = _scan_input(message, self.config)
            self._append_session(thread_id, "user", message)

            full_response: list[str] = []
            for chunk in self.agent.stream(
                {"messages": [{"role": "user", "content": message}]},
                config=self._invoke_config(thread_id),
                stream_mode="messages",
            ):
                if isinstance(chunk, tuple):
                    msg, meta = chunk
                    if hasattr(msg, "content") and isinstance(msg.content, str):
                        full_response.append(msg.content)
                        yield msg.content
                elif hasattr(chunk, "content"):
                    full_response.append(chunk.content)
                    yield chunk.content

            full = "".join(full_response)
            full = _scan_output(full, self.config)
            self._append_session(thread_id, "assistant", full, extra=self._model_stamp(self.config))

    # ----------------------------------------------------------------- async

    async def ainvoke(
        self,
        message: str,
        thread_id: str | None = None,
        *,
        override_model: str | None = None,
        override_reasoning_effort: str | None = None,
    ) -> str:
        """Run one turn and return the reply text.

        ``override_model`` / ``override_reasoning_effort`` are the per-turn
        overrides the dashboard chat's model and effort chips set, forwarded
        to :meth:`astream_events` unchanged (a transient agent cached per
        pair, the thread history preserved). A scheduled job carries them so
        one job can run on a different model or at a different effort than
        the agent's configured default.

        A thin wrapper over :meth:`astream_events`, exactly as :meth:`astream`
        is, so every non-streaming caller (the scheduler and its heartbeat,
        issue and Gmail polls, webhooks, the REST ``/api/chat``, inbound MCP
        and A2A, ``talk_to_agent`` peers, the onboarding greeting) runs the
        same turn the dashboard chat runs: the same HITL loop, the same
        end-of-turn bookkeeping, and the same transcript rows. It used to
        drive the graph itself and persisted only a ``user`` and an
        ``assistant`` row, so a scheduled run's tool calls, tool results and
        ``write_todos`` plan never reached the thread view: the dashboard
        showed the prompt and the final answer and nothing in between, while
        the identical turn typed into chat drew every step live and on reload.

        No ``approval_handler`` is passed, so an interrupt resolves by the
        call's origin (scheduled → approve, cognition → switch, human/headless
        → defer to the approvals inbox), as before. A peer turn is recorded as
        ``peer_inbound`` / ``via_peer`` rows and its optional ``a2a_preamble``
        is injected as a transient first-turn system message, both inside the
        stream path. A reply the model declined under
        ``message_policy.ignore_rules`` comes back as ``""``. An override
        resolves through ``_agent_for_model`` (same checkpointer, shared agent
        and config never mutated); usage, credits and the transcript's model
        stamp are attributed to the model that ran, and a model that cannot be
        built raises ``AgentError`` naming it, so a scheduler run records *why*
        it did not happen instead of silently falling back to the default.
        """
        from olano.tools._credentials import agent_runtime_context

        thread_id = thread_id or str(uuid.uuid4())
        parts: list[str] = []
        declined = False
        with (
            # Detach from any caller's live stream (e.g. talk_to_agent invoking a
            # peer mid-turn) so this agent's tokens never leak into the caller's
            # channel reply. No-op for top-level callers. See the helper's docstring.
            _detached_runnable_config(),
            # Same shape for the egress lists: `push_request_context` merges, so
            # a peer invoked mid-turn would otherwise run its model calls under
            # the CALLING agent's lists. This agent's own tools re-arm
            # enforcement under its own policy (egress_guard.governed).
            _egress_exempt(),
            # The stream path binds identity and the credential overlay itself;
            # this outer binding adds `credential_scope`, so a worker calling
            # `ainvoke` for its parent keeps resolving the parent's credentials.
            agent_runtime_context(
                self.config.name,
                self.config.env,
                credential_scope=self.config.credential_scope,
            ),
        ):
            async for ev in self.astream_events(
                message,
                thread_id=thread_id,
                override_model=override_model or None,
                override_reasoning_effort=override_reasoning_effort or None,
            ):
                if not (isinstance(ev, tuple) and len(ev) == 2):
                    continue
                kind, payload = ev
                if kind == "text" and isinstance(payload, str):
                    parts.append(payload)
                elif kind == "message_policy":
                    # The model declined: "" is how every non-streaming caller
                    # says "send nothing" (the scheduler's history row falls
                    # back to "completed", a messenger sends no reply).
                    declined = True
        if declined:
            # INFO with the origin: a decline on a scheduled or cognition turn
            # means the model applied a chat-message rule to a task prompt
            # despite the block's scoping, which is the thing an operator
            # needs to be able to find.
            logger.info(
                "ainvoke: reply suppressed by message_policy.ignore_rules (agent=%s origin=%s)",
                self.config.name,
                getattr(current_origin(), "value", current_origin()),
            )
            return ""
        # The streamed chunks are raw; the persisted row went through
        # `_scan_output` in `_append_assistant_text`, and so does the reply.
        return _scan_output("".join(parts), self.config)

    async def astream(
        self,
        message: str,
        thread_id: str | None = None,
        session_mode: str | None = None,
    ) -> AsyncIterator[str]:
        """Yield assistant text chunks.

        Thin wrapper over :meth:`astream_events` so the full tool-interrupt /
        HITL loop and end-of-turn bookkeeping run. Previously this method
        streamed the raw graph directly and had **no** interrupt handling, so a
        tool that required approval silently truncated the stream. Delegating
        here fixes that for every plain-text caller (CLI ``--stream``, etc.);
        non-text events are dropped for this text-only stream.
        """
        async for kind, payload in self.astream_events(
            message, thread_id=thread_id, session_mode=session_mode
        ):
            if kind == "text" and isinstance(payload, str):
                yield payload

    # ----------------------------------------------------------------- utils

    @staticmethod
    def _is_ai_message(msg: Any) -> bool:
        msg_type = getattr(msg, "type", None)
        if msg_type in ("ai", "AIMessage", "AIMessageChunk"):
            return True
        cls_name = type(msg).__name__
        return cls_name in ("AIMessage", "AIMessageChunk")

    @staticmethod
    def _extract_content(msg: Any) -> str:
        content = getattr(msg, "content", None)
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif isinstance(block, str):
                    parts.append(block)
            return "".join(parts)
        return ""

    @staticmethod
    def _is_internal_chunk(msg: Any, stream_meta: Any) -> bool:
        """True when a streamed chunk is internal machinery, not the answer.

        Two independent markers, because the two sources mark themselves
        differently:

        **1. The compaction middleware.** deepagents' ``SummarizationMiddleware``
        compacts long threads by calling the model with LangChain's
        ``DEFAULT_SUMMARY_PROMPT`` (the "## SESSION INTENT / ## SUMMARY /
        ## ARTIFACTS / ## NEXT STEPS" template). That call runs in the root
        namespace and its only marker is ``lc_source="summarization"`` metadata —
        it is NOT tagged ``nostream``. Under ``stream_mode="messages"``
        its tokens therefore surface as ordinary root-namespace
        ``AIMessageChunk``s and, unfiltered, get spliced into the agent's reply
        (they even land *before* the real answer, since summarization runs first
        in the turn).

        **2. A model call made inside the graph** — ``image_describe``'s vision
        call, a ``delegate_task`` worker. Those inherit the turn's streaming
        callbacks unless detached, and two of them running concurrently (one
        photo burst, two ``image_describe`` calls) interleave two answers into
        one unreadable reply. :mod:`olano.core.stream_isolation` detaches them at
        the source and tags them ``nostream``; this is the third belt,
        so a future in-graph model call that forgets the tag still cannot reach a
        channel just by being un-detached.

        Checks the stream metadata (``data[1]`` from LangGraph's messages mode)
        and, defensively, the message's own metadata attributes, since a
        marker's exact landing spot depends on LangGraph/LangChain versions.
        """

        def _lc_source(obj: Any) -> Any:
            return obj.get("lc_source") if isinstance(obj, dict) else None

        if _lc_source(stream_meta) == "summarization":
            return True
        if has_nostream_tag(stream_meta):
            return True
        for attr in ("response_metadata", "additional_kwargs", "metadata"):
            value = getattr(msg, attr, None)
            if _lc_source(value) == "summarization" or has_nostream_tag(value):
                return True
        return False

    @staticmethod
    def _message_role(msg: Any) -> str:
        if isinstance(msg, dict):
            role = msg.get("role") or msg.get("type") or ""
            return str(role).lower()
        for attr in ("role", "type"):
            value = getattr(msg, attr, None)
            if isinstance(value, str) and value:
                return value.lower()
        return ""

    @classmethod
    def _extract_response(cls, result: Any) -> str:
        messages = result.get("messages", [])
        search_messages = messages

        # Deepagents returns full thread state. Restrict extraction to assistant
        # messages that appear after the latest user turn so we never replay a
        # stale assistant response from earlier in the thread.
        for idx in range(len(messages) - 1, -1, -1):
            role = cls._message_role(messages[idx])
            if role in {"user", "human"}:
                search_messages = messages[idx + 1 :]
                break

        # Walk backward and return the last AI message that has non-empty
        # content. A model that ends its turn with only tool_calls produces an
        # empty AIMessage as its terminal output — in that case we keep
        # walking and surface the most recent assistant text from the same
        # turn (e.g. text written before a tool call, or after a subagent
        # delegation returned).
        last_tool_text = ""
        for msg in reversed(search_messages):
            role = cls._message_role(msg)
            if role == "ai":
                content = getattr(msg, "content", "")
                if isinstance(content, str) and content.strip():
                    return content
                if isinstance(content, list):
                    parts = []
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(block.get("text", ""))
                        elif isinstance(block, str):
                            parts.append(block)
                    joined = "".join(parts)
                    if joined.strip():
                        return joined
            elif isinstance(msg, dict) and role == "assistant":
                ct = msg.get("content", "")
                if ct and str(ct).strip():
                    return cast(str, ct)
            elif role == "tool" and not last_tool_text:
                # Remember the most recent tool output as a last-resort
                # fallback when the conductor delegates and stops without a
                # summary.
                tool_content = getattr(msg, "content", None)
                if tool_content is None and isinstance(msg, dict):
                    tool_content = msg.get("content", "")
                if isinstance(tool_content, str) and tool_content.strip():
                    last_tool_text = tool_content
        return last_tool_text

    def load_session(self, thread_id: str) -> list[dict[str, str]]:
        path = self._session_path(thread_id)
        if not path.exists():
            return []
        entries = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "session %s line %d: skipping malformed JSON (%s)",
                    thread_id,
                    line_no,
                    exc,
                )
                continue
            # Tolerant migration: legacy entries (pre v1) had no schema_version field.
            # Treat them as v0 in memory; we never rewrite the file, so disk shape
            # is preserved for forensic / audit reasons.
            entry.setdefault("schema_version", 0)
            entries.append(entry)
        return entries

    @staticmethod
    def _session_entries_to_messages(entries: list[dict[str, str]]) -> list[Any]:
        """Rebuild LangChain messages from a JSONL transcript (resume/fork).

        Only ``user``/``assistant``/``system`` roles are reconstructed; ``tool_call``
        and ``tool_result`` entries are intentionally dropped. The JSONL does not store
        a stable ``tool_call_id`` to pair calls to results, and an unpaired
        tool_use/tool_result sequence is rejected by Anthropic/OpenAI — so a text-only
        history is the safe degraded form. (The live checkpoint, which keeps fully
        paired ToolMessages, is the source of truth whenever it exists.)
        """
        messages: list[Any] = []
        for entry in entries:
            role = entry.get("role", "")
            content = entry.get("content", "")
            if not content:
                continue
            if role == "user":
                messages.append(HumanMessage(content=content))
            elif role == "assistant":
                messages.append(AIMessage(content=content))
            elif role == "system":
                messages.append(SystemMessage(content=content))
        return messages

    async def ensure_thread_loaded(self, thread_id: str) -> list[dict[str, str]]:
        """
        Hydrate persisted JSONL session history into graph state when resuming
        across a fresh process, then return the stored transcript entries.
        """
        entries = self.load_session(thread_id)
        if not entries:
            return []

        try:
            current_state = await self.agent.aget_state(self._invoke_config(thread_id))
            current_values = getattr(current_state, "values", {}) or {}
            current_messages = (
                current_values.get("messages", []) if isinstance(current_values, dict) else []
            )
            if current_messages:
                return entries
        except Exception:
            current_messages = []

        messages = self._session_entries_to_messages(entries)
        if not messages:
            return entries

        try:
            await self.agent.aupdate_state(self._invoke_config(thread_id), {"messages": messages})
        except Exception as exc:
            logger.debug("Resume hydrate skipped for thread %s: %s", thread_id, exc)
        return entries

    async def fork_thread(
        self,
        source_thread_id: str,
        *,
        carry_history: bool = True,
        carry_state: bool = True,
    ) -> str:
        """
        Fork a fresh thread for the current agent.

        When carry_history=True, only the visible persisted transcript is copied.
        Internal startup/middleware turns are intentionally excluded because they are
        not written into the JSONL session log.
        """
        new_thread_id = str(uuid.uuid4())

        if carry_history:
            entries = self.load_session(source_thread_id)
            messages = self._session_entries_to_messages(entries)
            if messages:
                try:
                    await self.agent.aupdate_state(
                        self._invoke_config(new_thread_id), {"messages": messages}
                    )
                except Exception as exc:
                    logger.debug(
                        "Thread fork hydrate skipped for %s -> %s: %s",
                        source_thread_id,
                        new_thread_id,
                        exc,
                    )
                self._write_session_entries(new_thread_id, entries)

        if carry_state:
            state = self.load_thread_state(source_thread_id)
            if state:
                self.save_thread_state(new_thread_id, state)

        return new_thread_id

    async def compact_thread(self, thread_id: str) -> bool:
        """Compact a thread on demand by asking the agent to invoke deepagents'
        ``compact_conversation`` tool.

        deepagents' ``SummarizationToolMiddleware`` exposes ``compact_conversation``
        (added to every agent in the builder). It summarizes older messages, offloads
        the full history to ``conversation_history/<thread>.md``, and keeps the recent
        tail (including ToolMessages) — the model-aware, tool-safe path. This replaces
        the old hand-rolled thread-forking compaction.

        Returns True only if the conversation was actually compacted (the tool reports
        "Nothing to compact yet" when the thread is still within the token budget).
        """
        directive = (
            "Call the `compact_conversation` tool now to summarize older messages and "
            "free up context window space. Do not take any other action."
        )
        compacted = False
        async for ev in self.astream_events(directive, thread_id=thread_id, internal_message=True):
            # The deepagents compact tool returns "Summarized N messages into a concise
            # summary." on success and "Nothing to compact yet …" when within budget;
            # match the success phrase (the tool_result `name` is not always populated).
            if (
                isinstance(ev, tuple)
                and len(ev) == 2
                and ev[0] == "tool_result"
                and isinstance(ev[1], dict)
                and "into a concise summary" in str(ev[1].get("content", ""))
            ):
                compacted = True
        return compacted

    async def estimate_thread_context_tokens(self, thread_id: str) -> int | None:
        """Estimated size of what the model will see on the thread's next turn.

        Reads the checkpoint, applies the ``_summarization_event`` the way
        deepagents does (summary message + everything from its cutoff on) and
        prices the result with the ``/context`` heuristic. Compaction never
        deletes messages from state, so counting ``state["messages"]`` raw
        would report the pre-compaction size forever. ``None`` when the thread
        has no checkpoint or the state cannot be read; never raises.
        """
        try:
            state = await self.agent.aget_state(self._invoke_config(thread_id))
        except Exception:
            return None
        values = getattr(state, "values", None) or {}
        messages = list(values.get("messages") or [])
        event = values.get("_summarization_event")
        if isinstance(event, dict):
            cutoff = event.get("cutoff_index")
            summary = event.get("summary_message")
            if isinstance(cutoff, int) and 0 <= cutoff <= len(messages):
                messages = ([summary] if summary is not None else []) + messages[cutoff:]
        from olano.runtime.context_report import thread_message_tokens

        return thread_message_tokens(messages)

    # ----------------------------------------------------------------- rich events

    @classmethod
    def _extract_last_ai_text(cls, current_state: Any) -> str:
        """Return the text content of the most recent assistant message in state.

        Used as a fallback after a Command(resume=...) cycle where streaming
        may not have produced any chunked text (the checkpoint still holds the
        final AIMessage written by the graph).
        """
        values = getattr(current_state, "values", None) or {}
        if not isinstance(values, dict):
            return ""
        messages = values.get("messages", [])
        for msg in reversed(messages):
            role = cls._message_role(msg)
            if role in {"user", "human"}:
                break
            if role == "ai":
                text = cls._extract_content(msg)
                if text.strip():
                    return text
        return ""

    @staticmethod
    def _extract_interrupt_actions(current_state: Any) -> list[dict[str, Any]]:
        """Normalize deepagents/LangGraph interrupt payloads into action requests."""
        actions: list[dict[str, Any]] = []
        for interrupt in getattr(current_state, "interrupts", []) or []:
            interrupt_value = getattr(interrupt, "value", None)
            if not isinstance(interrupt_value, dict):
                continue
            for action in interrupt_value.get("action_requests", []) or []:
                if not isinstance(action, dict):
                    continue
                normalized = dict(action)
                normalized["interrupt_id"] = getattr(interrupt, "id", "")
                actions.append(normalized)
        return actions

    async def astream_events(
        self,
        message: str | list,
        thread_id: str | None = None,
        approval_handler: Any | None = None,
        internal_message: bool = False,
        session_mode: str | None = None,
        override_model: str | None = None,
        override_reasoning_effort: str | None = None,
        turn_directive: str | None = None,
        turn_id: str | None = None,
    ):
        """Run one streamed turn; see :meth:`_astream_events_impl` for the events.

        This wrapper owns the thread's live-activity frames (``thinking`` →
        throttled ``streaming`` previews → ``idle``) so every surface that
        streams a turn — dashboard, platforms, the OpenAI connector — announces
        it the same way, and a chat open on another device can show the agent
        working on the thread while it happens. The thread id is settled here
        so the frames and the transcript rows name the same conversation.
        """
        from olano.core import thread_locks
        from olano.core.steering import REGISTRY as _steering
        from olano.core.thread_events import StreamPreview

        thread_id = thread_id or str(uuid.uuid4())
        preview = StreamPreview(self.config.name, thread_id, channel=self._turn_channel())
        # Mid-turn steering (core/steering.py): register this turn so
        # `steer()` can hand it a message while it runs. `turn_id` is the
        # client's handle on THIS turn (the dashboard learns it from the
        # `start` frame); a steer naming another turn is refused. Internal
        # turns (compaction directives) are never steerable.
        steer_handle = (
            None if internal_message else _steering.begin_turn(self.config.name, thread_id, turn_id)
        )
        self._publish_activity(thread_id, "thinking")
        effective_turn_id = (
            steer_handle.turn_id if steer_handle is not None else (turn_id or uuid.uuid4().hex)
        )
        turn_token = _TURN_ID.set(effective_turn_id)
        # Opt-in Langfuse tracing (olano/observability): one trace per turn,
        # active for the whole impl so graph runs (`_run_config`) and detached
        # in-graph model calls (`tracing.nested_config`) land under it. Never
        # raises; `(None, None)` when the turn is not traced.
        trace, trace_token = self._begin_turn_trace(
            thread_id,
            effective_turn_id,
            internal_message=internal_message,
            override_model=override_model,
        )
        last_usage: dict[str, Any] = {}
        turn_error: BaseException | None = None
        # The checkpoint sweep's compaction appends a checkpoint on this
        # thread's tip. It takes this lock without waiting and skips the thread
        # while a turn holds it, and a turn arriving while it publishes waits
        # the milliseconds that takes, so a turn can never resume into a tip
        # the sweep is about to replace (core/thread_locks.py). Taken last
        # before the try so the finally below always releases it.
        turn_lock = await thread_locks.acquire(scope_thread(self.config.name, thread_id))
        try:
            async for ev in self._astream_events_impl(
                message,
                thread_id=thread_id,
                approval_handler=approval_handler,
                internal_message=internal_message,
                session_mode=session_mode,
                override_model=override_model,
                override_reasoning_effort=override_reasoning_effort,
                turn_directive=turn_directive,
                steer_handle=steer_handle,
            ):
                if isinstance(ev, tuple) and len(ev) == 2 and ev[0] == "text":
                    try:
                        preview.feed(ev[1] if isinstance(ev[1], str) else "")
                    except Exception:
                        logger.debug("stream preview skipped", exc_info=True)
                elif (
                    trace is not None
                    and isinstance(ev, tuple)
                    and ev[0] == "usage"
                    and isinstance(ev[1], dict)
                ):
                    last_usage = ev[1]
                yield ev
        except BaseException as exc:
            turn_error = exc
            raise
        finally:
            turn_lock.release()
            if trace is not None or trace_token is not None:
                self._end_turn_trace(trace, trace_token, last_usage, turn_error)
            try:
                _TURN_ID.reset(turn_token)
            except ValueError:
                # Closed from another context (a generator finalized by GC):
                # that context never saw the value, so there is nothing to undo.
                pass
            try:
                preview.flush()
            except Exception:
                logger.debug("stream preview flush skipped", exc_info=True)
            if steer_handle is not None:
                leftovers = _steering.end_turn(steer_handle)
                if leftovers:
                    # Accepted but never read: the turn ended before its next
                    # model call. The dashboard still holds them in its queue
                    # and sends them as the next turn — nothing is lost here.
                    logger.info(
                        "steer: %d message(s) undelivered when turn %s ended on %s/%s",
                        len(leftovers),
                        steer_handle.turn_id,
                        self.config.name,
                        thread_id,
                    )
            self._publish_activity(thread_id, "idle")

    def _begin_turn_trace(
        self,
        thread_id: str,
        turn_id: str,
        *,
        internal_message: bool,
        override_model: str | None,
    ) -> tuple[Any, Any]:
        """Start this turn's Langfuse trace when its policy says so."""
        try:
            from olano.observability.policy import effective_trace_policy

            policy = effective_trace_policy(self.config)
            if policy is None:
                return None, None
            from olano.models.olano_gateway import is_olano_model
            from olano.observability import tracing
            from olano.observability.ids import turn_seed
            from olano.tools._credentials import _build_overlay

            ctx = get_request_context()
            origin = _trace_origin(ctx)
            if origin in tracing.BACKGROUND_ORIGINS and not policy.trace_background:
                return None, None
            channel = self._turn_channel()
            model = str(override_model or self.config.model or "")
            tier = model if is_olano_model(model) else ""
            scoped = scope_thread(self.config.name, thread_id)
            name = (
                "internal" if internal_message else ("turn" if origin == "interactive" else origin)
            )
            tags = [f"agent:{policy.agent}", f"origin:{origin}"]
            if channel:
                tags.append(f"channel:{channel}")
            trace, token = tracing.begin_run(
                policy,
                seed=turn_seed(self.config.name, scoped, turn_id),
                name=name,
                session_id=scoped,
                sender=str(ctx.user_id or ""),
                tags=tags,
                metadata={
                    "olano_agent": self.config.name,
                    "olano_origin": origin,
                    "olano_channel": channel,
                    "olano_model": model,
                    "olano_turn_id": turn_id,
                    "olano_internal": True if internal_message else None,
                },
                tier=tier,
                env_overlay=_build_overlay(self.config.env),
            )
            if trace is not None:
                self._last_trace_ids[thread_id] = trace.trace_id
                # Bounded: one entry per recently traced thread is all the
                # chat route's `done` frame needs.
                while len(self._last_trace_ids) > 256:
                    self._last_trace_ids.pop(next(iter(self._last_trace_ids)))
            return trace, token
        except Exception:
            logger.debug("turn trace not started", exc_info=True)
            return None, None

    def last_trace_id(self, thread_id: str) -> str:
        """The Langfuse trace id of ``thread_id``'s most recent traced turn on
        this runner, or ``""``. Read by the chat route for its ``done`` frame
        so the dashboard can link a reply the moment it lands."""
        return self._last_trace_ids.get(thread_id, "")

    @staticmethod
    def _end_turn_trace(
        trace: Any, token: Any, usage: dict[str, Any], error: BaseException | None
    ) -> None:
        """Close the turn's trace with its usage; a stop is not an error."""
        try:
            from olano.observability import tracing

            stopped = isinstance(error, GeneratorExit | asyncio.CancelledError)
            metadata = {
                "olano_credits": usage.get("credits"),
                "olano_usd": usage.get("usd"),
                "olano_input_tokens": usage.get("input_tokens"),
                "olano_output_tokens": usage.get("output_tokens"),
                "olano_model_calls": usage.get("model_calls"),
                "olano_stopped": True if stopped else None,
            }
            tracing.end_run(trace, token, metadata=metadata, error=None if stopped else error)
        except Exception:
            logger.debug("turn trace not closed", exc_info=True)

    async def _astream_events_impl(
        self,
        message: str | list,
        thread_id: str | None = None,
        approval_handler: Any | None = None,
        internal_message: bool = False,
        session_mode: str | None = None,
        override_model: str | None = None,
        override_reasoning_effort: str | None = None,
        turn_directive: str | None = None,
        steer_handle: Any | None = None,
    ):
        """
        Yield typed stream events for rich display:
            ("text",          str)
            ("subagent_text", {"text": str, "namespace": tuple, "agents": list[str]?})
            ("tool_call",     {"id": str, "name": str, "args": str})
            ("tool_result",   {"id": str, "name": str, "content": str})
            ("approval",      {"actions": list[dict]})
            ("status",        {"kind": str, "message": str})
            ("usage",         {"input_tokens": int, "output_tokens": int, "context_tokens": int,
                               "model_calls": int, ...})
            ("thread_switch", {"old_thread_id": str, "new_thread_id": str, "reason": str})
            ("steer",         {"id": str, "text": str, "sender": str})  # a mid-turn message
                               the model just read (core/steering.py); emitted before
                               anything the next model call produces

        ``steer_handle`` is the registry handle ``astream_events`` opened for
        this turn (``None`` for internal turns): steers the middleware drains
        are reported through it — see ``_flush_steers`` below.

        ``session_mode`` is the ephemeral chat-session override of the agent's
        persistent Oversight profile. ``"auto"``/``"act"`` mean "act freely" —
        every interrupted tool call is approved and no ``("approval", …)`` frame
        is emitted. ``"manual"``/``"plan"``/``None`` honor the agent's configured
        approval set and route each interrupt through ``approval_handler``.

        ``turn_directive`` is ephemeral per-turn guidance (the Plan-mode
        preamble) prepended to the model input for this turn ONLY — it is never
        written to the JSONL session, so the dashboard's thread history shows the
        user's original message rather than the directive.

        ``override_model`` (e.g. ``"anthropic:claude-sonnet-4-6"``) runs this
        turn against a different model than the agent's configured one, via a
        transient agent that reuses the same checkpointer (thread history is
        preserved). ``None`` / the configured model → the shared agent. Usage is
        attributed to whichever model actually ran.
        """
        from langgraph.types import Command

        from olano.core.steering import REGISTRY as _steering

        _begin_deployment_turn(self.config)
        thread_id = thread_id or str(uuid.uuid4())
        # Consume any queued MCP online/offline note so the agent learns of the
        # change at the start of this turn (injected once, below).
        mcp_notice = self._pending_mcp_notice
        self._pending_mcp_notice = None
        # Single per-turn read of <thread>.state.json, shared by the request
        # metadata and the prompt-change gate below.
        thread_state = self.load_thread_state(thread_id)
        # One-shot "your operating instructions changed" notice when the stable
        # prompt sections moved since this thread last ran. Skipped for
        # internal turns (compaction directives, session-start tokens) so
        # synthetic traffic never consumes or pollutes the update.
        prompt_notice = (
            None if internal_message else self._prompt_change_notice(thread_id, thread_state)
        )
        # Context-only events recorded since the last turn (owner interjections
        # in secretary chats, proactive notifications delivered to this chat).
        # Internal turns (compaction directives, session-start tokens) leave
        # them queued for the next real turn.
        context_notice = (
            None if internal_message else self._consume_context_notes(thread_id, thread_state)
        )
        initial_metadata = self._request_context_metadata(thread_id, state=thread_state)
        if isinstance(message, str):
            peer_target = _explicit_peer_target_from_message(message, self.config.name)
            if peer_target:
                initial_metadata["explicit_peer_target"] = peer_target
        request_ctx = push_request_context(
            session_id=thread_id,
            metadata=initial_metadata,
        )
        request_ctx.__enter__()
        # Who is asking. A peer turn (`talk_to_agent`, the ACP bridge) is
        # recorded as `peer_inbound` / `via_peer` rows so the Agent to Agent
        # view can attribute it, and an opt-in per-pair A2A digest rides along
        # as a transient first-turn system message that is deliberately NOT
        # written to the JSONL transcript (which stays the raw peer message)
        # so the persisted record never bloats. Internal turns (compaction
        # directives) carry neither.
        turn_ctx = get_request_context()
        peer_sender = turn_ctx.user_id if turn_ctx.source == "peer" else ""
        a2a_preamble = (
            "" if internal_message else str((turn_ctx.metadata or {}).get("a2a_preamble") or "")
        )
        # Bind the agent's config name into the contextvar so tools like
        # scheduler_create can attribute ownership to the correct system name
        # regardless of what display name the agent uses in conversation.
        # This covers CLI paths — api.py and server.py set it earlier,
        # but setting it here is idempotent and ensures all code paths work.
        from olano.tools.talk_to import current_agent as _ca

        _ca_runner_token = _ca.set(self.config.name)
        from olano.tools._agent_ctx import AGENT_CTX as _google_agent_ctx

        # The Google token scope follows `credential_scope` like every other
        # credential layer does (`agent_runtime_context`): a worker acting for
        # its parent reads the parent's tokens, not a store keyed on its own
        # throwaway name.
        _google_ctx_token = _google_agent_ctx.set(
            (self.config.credential_scope or "").strip() or self.config.name
        )
        # Bind this agent's credential overlay (config.env with ${vault:...}
        # resolved) so native toolkits resolve per-agent accounts for this turn.
        from olano.tools._credentials import _build_overlay
        from olano.tools._credentials import current_agent_env as _agent_env

        _agent_env_token = _agent_env.set(_build_overlay(self.config.env))
        # Multimodal payloads (e.g. dashboard image attachments) arrive as a
        # list of content blocks like [{"type":"text","text":"..."},
        # {"type":"image","source":...}]. Skip middleware/scan on those —
        # the input scanner only knows how to operate on strings — but pull
        # out the joined text portion for the JSONL session log so the
        # transcript stays human-readable.
        is_multimodal = isinstance(message, list)
        if is_multimodal:
            text_parts = [
                block.get("text", "")
                for block in message
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            raw_message = "\n".join(text_parts).strip()
            hook_context = self._hook_context(thread_id)
            hook_context["internal_message"] = internal_message
        else:
            assert isinstance(message, str)
            raw_message = message
            if not internal_message:
                raw_message = _scan_input(message, self.config)
                message = raw_message
            hook_context = self._hook_context(thread_id)
            hook_context["internal_message"] = internal_message
            # Run middleware on_input hooks
            if self._mw_hooks and not self._mw_hooks.is_empty():
                try:
                    from olano.core.middleware_loader import InputBlockedError, run_input_hooks

                    message = await run_input_hooks(
                        message, self._mw_hooks, self.config, hook_context
                    )
                except InputBlockedError as e:
                    raise AgentError(f"Message blocked by middleware: {e}") from e
                except Exception:
                    pass
        if not internal_message:
            if peer_sender:
                self.append_session_entry(
                    thread_id,
                    {"role": "peer_inbound", "from": peer_sender, "content": raw_message},
                )
            else:
                self._append_session(thread_id, "user", raw_message)

        # Ephemeral per-turn guidance (Plan mode): seen by the model this turn,
        # but logged above as the original message so chat history stays clean.
        if turn_directive:
            if isinstance(message, str):
                message = f"{turn_directive}\n\n{message}"
            elif isinstance(message, list):
                message = [{"type": "text", "text": turn_directive}, *message]

        pending: dict[str, dict] = {}
        suppressed_tool_ids: set[str] = set()
        _idx_to_id: dict[str, str] = {}  # chunk index → tool_call_id
        full_text: list[str] = []
        # The answer text as ordered segments, each tagged with the tool call it
        # resumed after (``None`` for the opening one). The dashboard renders
        # tool cards between segments instead of stacking every card above
        # the whole reply; ``_take_text`` maintains it alongside ``full_text``.
        segments: list[dict[str, Any]] = []
        break_pending = False
        last_tool_id: str | None = None

        def _note_tool(tool_id: Any) -> None:
            nonlocal break_pending, last_tool_id
            break_pending = True
            if tool_id:
                last_tool_id = str(tool_id)

        # `message_policy.ignore_rules` asks the model to decline by writing a
        # marker instead of a reply. It must never reach a consumer: the
        # dashboard would draw it, a messenger would send it, and the JSONL
        # transcript would keep it. The filter holds at most the marker's length
        # while it decides, then becomes a pass-through — so a normal turn pays
        # one `startswith` per chunk, and an agent with no rules pays nothing at
        # all (`None`). See security/reply_policy.SilenceFilter.
        silence: Any = None
        if getattr(self.config, "message_policy", None) and self.config.message_policy.ignore_rules:
            from olano.security.reply_policy import SilenceFilter

            silence = SilenceFilter()

        def _take_text(chunk: str) -> list[tuple[str, str]]:
            """Record *chunk* as answer text and return the events to yield.

            When text resumes after a tool boundary a blank line goes out first,
            so every consumer that concatenates chunks gets a paragraph break,
            and a new segment opens at the same point.
            """
            nonlocal break_pending
            if silence is not None:
                chunk = silence.feed(chunk)
                if not chunk:
                    # Held (still deciding) or dropped (declined). Either way
                    # nothing is recorded, so a silenced turn leaves no reply
                    # text behind — `full_text` stays empty and the `finally`
                    # writes no assistant row.
                    return []
            events: list[tuple[str, str]] = []
            if break_pending and _needs_paragraph_break(full_text):
                full_text.append("\n\n")
                events.append(("text", "\n\n"))
            if break_pending or not segments:
                segments.append({"text": "", "after": last_tool_id if break_pending else None})
            break_pending = False
            segments[-1]["text"] += chunk
            full_text.append(chunk)
            events.append(("text", chunk))
            return events

        # How much of `full_text` is already in the transcript as its own
        # assistant row (a steer splits the reply — see `_flush_steers`); the
        # `finally` persists only the remainder.
        flushed_upto = 0
        usage_meta: dict[str, int] = {}
        # `usage_meta["input_tokens"]` SUMS every model sub-call's input across the
        # turn (correct for billing/cost). The context-window *fill*, though, is the
        # size of the prompt on the LAST sub-call — not the sum — because each call's
        # prompt already contains the prior context. Track it separately (last-wins;
        # context grows monotonically within a turn) so the dashboard's context bar
        # doesn't read e.g. 433k against a 128k window after a tool-heavy turn.
        last_input_tokens = 0
        current_state = None
        user_decisions: list[dict[str, Any]] = []
        # The actions `user_decisions` answers, kept so the resume can be scoped
        # per interrupt id (see _resume_payload_for_actions).
        resume_actions: list[dict[str, Any]] = []
        thread_switch: dict[str, str] | None = None
        abort_current_turn = False
        last_finish_reason: str | None = None
        tool_iterations = 0
        budget_notices_emitted: set[int] = set()
        tool_failures: list[str] = []
        # Circuit breaker: count identical failing tool calls within this turn so a
        # genuinely stuck loop stops, while one-off recoverable errors flow back to
        # the model for it to diagnose and retry differently.
        tool_error_counts: dict[str, int] = {}
        tool_error_retry_limit = int(getattr(self.config, "tool_error_retry_limit", 3) or 0)
        # The middleware's view of the same thing, bound for the turn so it can
        # tell the model, on the failing result itself, that one more identical
        # failure ends the turn (see core/tool_failures.py). Reset in `finally`.
        _turn_failures_token = TURN_TOOL_FAILURES.set(
            TurnToolFailures(limit=tool_error_retry_limit)
        )
        # An attempt is one model step, not one tool result: three identical
        # parallel calls that fail together are one try the model has not seen
        # the answer to yet. Each call maps to the AI message that issued it,
        # and a (signature, step) pair counts once.
        tool_call_step: dict[str, str] = {}
        tool_error_counted: set[tuple[str, str]] = set()
        # Root tool results per step. When the breaker stops a turn mid-step,
        # LangGraph has not committed that step yet, so these are written back
        # explicitly (see _persist_breaker_stop).
        step_tool_messages: dict[str, list[Any]] = {}
        breaker_step: str | None = None
        breaker_text = ""

        def _breaker_step_complete() -> bool:
            """True once every call of the breaker's step has its result.

            The stream keeps being read (silently) until then: stopping at the
            first failure would leave the step's other parallel calls running
            and unrecorded, and a step is saved whole or not at all.
            """
            calls = {cid for cid, step in tool_call_step.items() if step == breaker_step}
            answered = {
                getattr(m, "tool_call_id", "")
                for m in step_tool_messages.get(breaker_step or "", [])
            }
            return calls <= answered

        did_resume = False

        async def _flush_steers(emit: bool):
            """Report the steers the model has read since the last check.

            Per steer: the assistant text streamed so far is persisted as its
            own row first, then the steer as a ``user`` row (``steer: true``),
            then — with ``emit`` — a ``("steer", …)`` event. That keeps the
            transcript in the order things happened (text, the aside, the
            continuation) instead of one reply row that swallows words said
            before the aside, which is also what lets a reload and a second
            device draw exactly what the sending tab drew. ``emit=False`` is
            for the ``finally``: a generator being closed may not yield.
            """
            nonlocal flushed_upto
            if steer_handle is None:
                return
            for sm in _steering.pop_delivered(steer_handle):
                segment = "".join(full_text[flushed_upto:])
                flushed_upto = len(full_text)
                if segment.strip():
                    await self._append_assistant_text(
                        thread_id, segment, hook_context, config=effective_config
                    )
                extra: dict[str, Any] = {"steer": True, "steer_id": sm.id}
                if sm.sender:
                    extra["sender"] = sm.sender
                self._append_session(thread_id, "user", sm.text, extra=extra)
                if emit:
                    yield ("steer", {"id": sm.id, "text": sm.text, "sender": sm.sender})

        provider_key = _provider_from_model(override_model or self.config.model)
        # Defaults so the finally block is safe even if the override build fails.
        agent = self.agent
        effective_config = self.config
        try:
            # Per-turn model override (dashboard picker): a transient agent bound
            # to the chosen model, reusing this agent's checkpointer. No override
            # → the shared agent/config, unchanged.
            agent, effective_config = await self._agent_for_model(
                override_model, override_reasoning_effort
            )
            # Computed once per turn: gates the gateway cost-header merge so a
            # BYOK model routed through someone's own proxy can't fabricate an
            # olano-credits charge from its cost telemetry.
            from olano.models.olano_gateway import is_olano_model as _is_olano

            managed_turn = _is_olano(effective_config.model)
            while current_state is None or getattr(current_state, "interrupts", None):
                is_resume = bool(user_decisions)
                if user_decisions:
                    resume_value = _resume_payload_for_actions(resume_actions, user_decisions)
                    payload: Command | dict[str, Any] = Command(resume=resume_value)
                else:
                    turn_messages: list[dict[str, Any]] = []
                    if a2a_preamble:
                        turn_messages.append({"role": "system", "content": a2a_preamble})
                        a2a_preamble = ""
                    if prompt_notice:
                        turn_messages.append({"role": "system", "content": prompt_notice})
                        prompt_notice = None
                    if mcp_notice:
                        turn_messages.append({"role": "system", "content": mcp_notice})
                        mcp_notice = None
                    if context_notice:
                        turn_messages.append({"role": "system", "content": context_notice})
                        context_notice = None
                    turn_messages.append({"role": "user", "content": message})
                    payload = {"messages": turn_messages}
                if is_resume:
                    did_resume = True
                    logger.debug(
                        "astream_events: resuming thread=%s with %d decision(s) across "
                        "%d interrupt(s) (id-scoped=%s): %s",
                        thread_id,
                        len(user_decisions),
                        len(resume_value),
                        "decisions" not in resume_value,
                        [d.get("type") for d in user_decisions],
                    )
                user_decisions = []
                resume_actions = []
                chunk_count = 0

                graph_stream = agent.astream(
                    payload,
                    config=self._run_config(thread_id),
                    stream_mode=["messages", "updates"],
                    subgraphs=True,
                )
                async for chunk in graph_stream:
                    chunk_count += 1
                    # A steer the middleware just appended: report it before
                    # anything the next model call produces (the steer node's
                    # own `updates` chunk wakes this loop ahead of the first
                    # token, which is why this sits above the `updates` skip).
                    async for ev in _flush_steers(emit=True):
                        yield ev
                    namespace = ()
                    stream_mode = "messages"
                    data = chunk

                    if isinstance(chunk, tuple) and len(chunk) == 3:
                        namespace, stream_mode, data = chunk
                    elif (
                        isinstance(chunk, tuple) and len(chunk) == 2 and isinstance(chunk[0], tuple)
                    ):
                        namespace, data = chunk
                        stream_mode = "messages"

                    if stream_mode == "updates":
                        continue

                    active_agent = _ca.get()
                    if active_agent and active_agent != self.config.name:
                        continue

                    is_subagent = bool(namespace)
                    msg = data[0] if isinstance(data, tuple) else data
                    stream_meta = data[1] if isinstance(data, tuple) and len(data) > 1 else None
                    is_internal = self._is_internal_chunk(msg, stream_meta)
                    cls = type(msg).__name__

                    if (
                        isinstance(stream_meta, dict)
                        and stream_meta.get("langgraph_node") == _HISTORY_PATCH_NODE
                    ):
                        # Re-emitted history, not this turn's output.
                        continue

                    if breaker_step is not None:
                        # The breaker has already spoken; only the rest of its
                        # step's results are still wanted, so they can be saved.
                        if cls == "ToolMessage" and not is_subagent:
                            drained_id = getattr(msg, "tool_call_id", "")
                            step_tool_messages.setdefault(
                                tool_call_step.get(drained_id) or drained_id, []
                            ).append(msg)
                        if _breaker_step_complete():
                            break
                        continue

                    if cls in ("AIMessage", "AIMessageChunk"):
                        if is_subagent:
                            # Subagent (`task` tool) model calls bill the same
                            # account as the parent's, so fold their usage and
                            # gateway charge into the turn's meta before
                            # skipping the rendering below — this used to
                            # `continue` first, and every subagent token was
                            # missing from the usage ledger while the mined
                            # (checkpoint) book counted it. Stays BELOW the
                            # active-agent check above: a peer callee's chunks
                            # are its own runner's to record. Known limit: the
                            # streamed estimate prices these tokens at the
                            # PARENT model's tier; the mined book keeps the
                            # real per-call models.
                            self._merge_olano_credits(usage_meta, msg, managed=managed_turn)
                            um = getattr(msg, "usage_metadata", None)
                            if um:
                                self._merge_usage_metadata(usage_meta, um)
                            text = self._extract_content(msg)
                            if text and not is_internal:
                                yield ("subagent_text", {"text": text, "namespace": namespace})
                            continue

                        for tc in getattr(msg, "tool_call_chunks", None) or []:
                            tc_id = tc.get("id") or ""
                            tc_idx = str(tc.get("index", 0))
                            if tc_id:
                                _idx_to_id[tc_idx] = tc_id
                                key = tc_id
                            else:
                                key = _idx_to_id.get(tc_idx) or tc_idx
                            if key not in pending:
                                pending[key] = {"name": "", "args": "", "announced": False}
                            if tc.get("name"):
                                pending[key]["name"] = tc["name"]
                            if tc.get("args"):
                                pending[key]["args"] += tc["args"]
                            entry = pending[key]
                            peer_call_pending = any(
                                p.get("name") in _PEER_TOOL_NAMES for p in pending.values()
                            )
                            if entry["name"] == "task" and peer_call_pending:
                                entry["suppressed"] = True
                                suppressed_tool_ids.add(key)
                                continue
                            if (
                                entry["name"] == "task"
                                and not entry.get("card_final")
                                and entry["args"]
                            ):
                                # The card goes out as soon as `subagent_type`
                                # is known, so the dashboard shows the subagent
                                # starting instead of nothing until it returns.
                                # The description is the subagent's whole brief
                                # and streams for a while; when the model emits
                                # `subagent_type` first, that early read has
                                # description "" (what the dashboard used to
                                # show for the rest of the run, and after a
                                # reload: the partial was the persisted row and
                                # the ToolMessage never re-announced). So the
                                # card is announced once from the partial text
                                # and corrected the moment the JSON closes: a
                                # second frame with the same id (the client
                                # upserts by id), and the ONE persisted row
                                # (analytics count rows) carries the full brief.
                                # Once announced, only a closed JSON is worth
                                # a read (the partial regex would re-scan the
                                # growing brief on every token for nothing).
                                card = (
                                    None
                                    if entry["announced"]
                                    and not entry["args"].rstrip().endswith("}")
                                    else _task_card_args(entry["args"])
                                )
                                if card is not None and (card[1] or not entry["announced"]):
                                    shown, complete = card
                                    if shown != entry.get("card"):
                                        entry["card"] = shown
                                        yield (
                                            "tool_call",
                                            {"id": key, "name": "task", "args": json.dumps(shown)},
                                        )
                                    if not entry["announced"]:
                                        _note_tool(key)
                                        entry["announced"] = True
                                    if complete:
                                        row_args = _task_row_args(shown)
                                        self._append_tool_session(
                                            thread_id,
                                            "tool_call",
                                            name="task",
                                            content=row_args,
                                            args=row_args,
                                        )
                                        entry["card_final"] = True
                            if (
                                entry["name"] in _PEER_TOOL_NAMES
                                and not entry["announced"]
                                and entry["args"]
                            ):
                                peer_partial = _peer_tool_args_from_stream(entry["args"])
                                if peer_partial:
                                    entry["peer_target"] = json.loads(peer_partial).get(
                                        "agent_name", ""
                                    )
                                    yield (
                                        "tool_call",
                                        {"id": key, "name": entry["name"], "args": peer_partial},
                                    )
                                    _note_tool(key)
                                    self._append_tool_session(
                                        thread_id,
                                        "tool_call",
                                        name=entry["name"],
                                        content=peer_partial,
                                    )
                                    entry["announced"] = True

                        for tc in getattr(msg, "tool_calls", None) or []:
                            key = tc.get("id", "")
                            tc_name = tc.get("name", "")
                            if key and getattr(msg, "id", None):
                                tool_call_step.setdefault(key, msg.id)
                            if key not in pending:
                                tc_args = tc.get("args", {})
                                args_str = (
                                    json.dumps(tc_args)
                                    if isinstance(tc_args, dict)
                                    else str(tc_args or "")
                                )
                                pending[key] = {
                                    "name": tc_name,
                                    "args": args_str,
                                    "announced": False,
                                }
                            elif not pending[key].get("name"):
                                pending[key]["name"] = tc_name
                            entry = pending[key]
                            peer_call_pending = any(
                                p.get("name") in _PEER_TOOL_NAMES for p in pending.values()
                            )
                            if tc_name == "task" and peer_call_pending:
                                entry["suppressed"] = True
                                suppressed_tool_ids.add(key)
                                continue
                            if tc_name in _PEER_TOOL_NAMES and not entry.get("announced"):
                                peer_partial = _peer_tool_args_from_stream(
                                    tc.get("args", entry.get("args", ""))
                                )
                                if peer_partial:
                                    entry["peer_target"] = json.loads(peer_partial).get(
                                        "agent_name", ""
                                    )
                                    yield (
                                        "tool_call",
                                        {"id": key, "name": tc_name, "args": peer_partial},
                                    )
                                    _note_tool(key)
                                    self._append_tool_session(
                                        thread_id,
                                        "tool_call",
                                        name=tc_name,
                                        content=peer_partial,
                                    )
                                    entry["announced"] = True
                            # Task tool: announce early from chunks path (above).
                            # All other tools: announcement deferred to ToolMessage
                            # where _TOOL_ARGS_REGISTRY gives guaranteed correct args
                            # (falls back to pending["args"] if registry has no entry).

                        # Olano gateway charge: rides response headers, which
                        # langchain attaches to each model sub-call's FIRST
                        # chunk — while usage_metadata arrives on its LAST. So
                        # this merge must be unconditional per AI chunk (never
                        # nested under `if um:`), or streamed managed turns
                        # ledger 0 credits. Accumulate-safe: it only adds when
                        # the header is present, once per sub-call.
                        self._merge_olano_credits(usage_meta, msg, managed=managed_turn)
                        um = getattr(msg, "usage_metadata", None)
                        if um:
                            self._merge_usage_metadata(usage_meta, um)
                            in_tok = um.get("input_tokens") if isinstance(um, dict) else None
                            if in_tok:
                                last_input_tokens = int(in_tok)
                        last_finish_reason = (
                            self._extract_finish_reason_from_message(msg) or last_finish_reason
                        )

                        text = self._extract_content(msg)
                        if text and not is_internal:
                            # While an announced talk_to_agent call is executing, any
                            # root-namespace text chunk belongs to the CALLEE's nested
                            # run (its model callbacks propagate into this stream), not
                            # to this agent. The caller's own model is suspended until
                            # the ToolMessage arrives, so this window is unambiguous.
                            # Surface it as subagent text so channel renderers don't
                            # splice another agent's words into this agent's reply.
                            peer_agents = [
                                str(p.get("peer_target") or "").strip()
                                for p in pending.values()
                                if p.get("name") in _PEER_TOOL_NAMES and p.get("announced")
                            ]
                            if peer_agents:
                                yield (
                                    "subagent_text",
                                    {
                                        "text": text,
                                        "namespace": ("peer",),
                                        "agents": [a for a in peer_agents if a],
                                    },
                                )
                            else:
                                for ev in _take_text(text):
                                    yield ev

                    elif cls == "ToolMessage":
                        if is_subagent:
                            continue
                        tid = getattr(msg, "tool_call_id", "")
                        tool_step = tool_call_step.get(tid) or tid
                        step_tool_messages.setdefault(tool_step, []).append(msg)
                        name = getattr(msg, "name", None) or pending.get(tid, {}).get(
                            "name", "tool"
                        )
                        # Default so the circuit-breaker signature is always defined;
                        # args_str is only assigned below when `tid in pending`.
                        args_str = ""
                        status = getattr(msg, "status", None)
                        content = self._extract_content(msg) or str(getattr(msg, "content", ""))
                        content = _scan_web_tool_result(name, content, self.config)
                        if self._mw_hooks and not self._mw_hooks.is_empty():
                            from olano.core.middleware_loader import run_tool_result_hooks

                            content = await run_tool_result_hooks(
                                name, content, self._mw_hooks, self.config, hook_context
                            )
                        if tid in pending:
                            tc_data = pending.pop(tid)
                            if tc_data.get("suppressed") or tid in suppressed_tool_ids:
                                suppressed_tool_ids.discard(tid)
                                continue
                            tc_name = tc_data["name"] or name

                            # Use the middleware registry as the authoritative args
                            # source — it captures them pre-execution from the actual
                            # request, bypassing all LLM streaming unreliability.
                            from olano.security.deepagents_middleware import (
                                _TOOL_ARGS_REGISTRY,
                            )

                            mw = _TOOL_ARGS_REGISTRY.pop(tid, None)
                            if mw:
                                args_str = json.dumps(mw["args"]) if mw["args"] else ""
                                if mw["name"] and not tc_name:
                                    tc_name = mw["name"]
                            else:
                                args_str = tc_data.get("args", "")

                            # Shell safety check always runs.
                            block_reason = _validate_shell_tool_call(tc_name, args_str, self.config)
                            if block_reason:
                                tool_failures.append(f"{tc_name}: {block_reason}")
                                if not tc_data.get("announced"):
                                    yield (
                                        "tool_call",
                                        {"id": tid, "name": tc_name, "args": args_str},
                                    )
                                yield (
                                    "tool_result",
                                    {
                                        "id": tid,
                                        "name": tc_name,
                                        "content": f"[blocked] {block_reason}",
                                    },
                                )
                                _note_tool(tid)
                                self._append_tool_session(
                                    thread_id,
                                    "tool_result",
                                    name=tc_name,
                                    content=f"[blocked] {block_reason}",
                                )
                                continue

                            tool_iterations += 1
                            for notice in self._maybe_budget_notice(
                                tool_iterations,
                                budget_notices_emitted,
                            ):
                                for ev in _take_text(notice):
                                    yield ev

                            if (
                                tc_name == "task"
                                and tc_data.get("announced")
                                and not tc_data.get("card_final")
                            ):
                                # Announced from a stream whose JSON never closed
                                # (cut short, or chunked without the closing
                                # brace), so no row was written and the card may
                                # still show the partial read: both take the
                                # args the middleware captured from the real
                                # request (`args_str`), falling back to what was
                                # announced.
                                card = _task_card_args(args_str)
                                shown = card[0] if card else dict(tc_data.get("card") or {})
                                if shown != tc_data.get("card"):
                                    yield (
                                        "tool_call",
                                        {"id": tid, "name": tc_name, "args": json.dumps(shown)},
                                    )
                                row_args = _task_row_args(shown)
                                self._append_tool_session(
                                    thread_id,
                                    "tool_call",
                                    name=tc_name,
                                    content=row_args,
                                    args=row_args,
                                )
                            if not tc_data.get("announced"):
                                yield ("tool_call", {"id": tid, "name": tc_name, "args": args_str})
                                _note_tool(tid)
                                self._append_tool_session(
                                    thread_id,
                                    "tool_call",
                                    name=tc_name,
                                    content=args_str,
                                )
                        yield ("tool_result", {"id": tid, "name": name, "content": content})
                        _note_tool(tid)
                        self._append_tool_session(
                            thread_id,
                            "tool_result",
                            name=name,
                            content=content,
                        )
                        # A [connector-auth] result is a refusal the runtime already
                        # tried to repair; it tells the model not to retry, and the
                        # breaker backs that up if the same call comes round again.
                        # The central error boundary's results carry status="success"
                        # (they are normal tool messages), so a failure is recognised
                        # by marker too, or an always-failing tool would loop.
                        is_error_result = is_failed_tool_result(status, content)
                        if not is_error_result and name in WRITE_TOOL_NAMES:
                            # The workspace changed: a command that failed before
                            # the edit is a new attempt after it, not a repeat.
                            tool_error_counts.clear()
                        if is_error_result:
                            tool_failures.append(f"{name}: {_tool_error_reason(content)}")
                            # Let the error flow back to the model so it can diagnose
                            # and try a different approach. Only stop the turn if the
                            # *identical* call keeps failing across model steps
                            # (unproductive loop).
                            if tool_error_retry_limit > 0:
                                sig = tool_call_signature(name, args_str)
                                if (sig, tool_step) not in tool_error_counted:
                                    tool_error_counted.add((sig, tool_step))
                                    tool_error_counts[sig] = tool_error_counts.get(sig, 0) + 1
                                if tool_error_counts[sig] >= tool_error_retry_limit:
                                    abort_current_turn = True
                                    breaker_step = tool_step
                                    forced_text = _repeated_failure_response(
                                        name, content, tool_error_counts[sig]
                                    )
                                    breaker_text = forced_text
                                    full_text = [forced_text]
                                    segments[:] = [{"text": forced_text, "after": last_tool_id}]
                                    yield ("text", forced_text)
                                    if _breaker_step_complete():
                                        break

                if breaker_step is not None:
                    await self._persist_breaker_stop(
                        agent,
                        thread_id,
                        graph_stream,
                        step_tool_messages.get(breaker_step, []),
                        breaker_text,
                    )

                # Deserializing the full thread checkpoint (aget_state) scales with
                # conversation length and is the dominant post-stream cost on long
                # threads. It only exists to detect tool-approval interrupts —
                # which come exclusively from tool calls in the interrupt_on map.
                # An un-executed (interrupted) tool call stays in ``pending`` after
                # the stream drains, so a pure-text turn leaves ``pending`` empty
                # and can never interrupt: skip the fetch on that hot path. We
                # still need real state on a resume that produced no streamed text
                # (the final AIMessage lives only in the checkpoint — see the
                # ``did_resume`` fallback below).
                needs_state = bool(pending) or is_resume or (did_resume and not full_text)
                if needs_state:
                    current_state = await agent.aget_state(self._invoke_config(thread_id))
                else:
                    current_state = _NO_INTERRUPT_STATE
                logger.debug(
                    "astream_events: stream exhausted — chunks=%d resume=%s full_text_len=%d "
                    "state_fetched=%s interrupts=%d",
                    chunk_count,
                    is_resume,
                    sum(len(t) for t in full_text),
                    needs_state,
                    len(getattr(current_state, "interrupts", None) or []),
                )
                actions = self._extract_interrupt_actions(current_state)
                if actions:
                    manual_indices: list[int]
                    if session_mode in ("auto", "act"):
                        # "Auto"/"Act" — act freely: approve every interrupted
                        # tool call and surface no approval card. The agent's
                        # configured approval set still decides *what* deepagents
                        # pauses on; these modes resolve each pause as approve.
                        # Except what only a person may approve (a browser
                        # submit, purchase or send): that card always shows.
                        manual_indices = [
                            idx for idx, action in enumerate(actions) if _always_asks_human(action)
                        ]
                    else:
                        manual_indices = _manual_action_indices(actions, self.config)
                    manual_actions = [actions[idx] for idx in manual_indices]

                    # Decisions map to actions BY POSITION, never by interrupt id.
                    # One interrupt carries an action_request per gated tool call
                    # of the same AI message — five parallel `task` delegations are
                    # ONE interrupt with five requests — so an id-keyed map
                    # collapses them onto whichever decision was written last:
                    # approve one and reject another and both came back rejected.
                    # Auto-approved actions are approved; manual ones start as
                    # reject — fail closed — until the handler's real decision
                    # overwrites them at their own index below.
                    user_decisions = [{"type": "approve"} for _ in actions]
                    for idx in manual_indices:
                        user_decisions[idx] = {"type": "reject"}

                    if manual_actions:
                        for action in manual_actions:
                            action.setdefault("risk", _classify_interrupt_action_risk(action))
                        if approval_handler:
                            # Interactive: surface the card and await the human.
                            yield ("approval", {"actions": manual_actions})
                            _note_tool(None)
                            manual_decisions = await approval_handler(manual_actions)
                        else:
                            # No interactive handler → resolve by call origin
                            # (scheduled/cognition/headless), not a blanket reject.
                            manual_decisions = await _resolve_autonomous_decisions(
                                manual_actions, self.config, current_origin()
                            )

                        normalized_decisions = list(manual_decisions[: len(manual_actions)])
                        while len(normalized_decisions) < len(manual_actions):
                            normalized_decisions.append({"type": "reject"})
                        # Both lists are exactly len(manual_actions) by
                        # construction, so strict=True is a free invariant check.
                        for idx, decision in zip(manual_indices, normalized_decisions, strict=True):
                            user_decisions[idx] = decision

                    # Answered together with the actions they belong to, so the
                    # resume can be scoped per interrupt id when more than one
                    # branch is parked (see _resume_payload_for_actions).
                    resume_actions = actions
                    abort_current_turn = any(
                        decision.get("type") == "replace_message" for decision in user_decisions
                    )

                    for action, decision in zip(actions, user_decisions, strict=False):
                        if decision.get("type") not in {"reject", "replace_message"}:
                            continue
                        block_message = str(
                            decision.get("message") or "Blocked by user approval policy."
                        )
                        yield (
                            "tool_result",
                            {
                                "id": action.get("interrupt_id", ""),
                                "name": action.get("name", "tool"),
                                "content": block_message,
                            },
                        )
                        _note_tool(action.get("interrupt_id", ""))
                        self._append_tool_session(
                            thread_id,
                            "tool_result",
                            name=action.get("name", "tool"),
                            content=block_message,
                        )

                if abort_current_turn:
                    break
                if not getattr(current_state, "interrupts", None):
                    break

            if not abort_current_turn:
                for tid, tc_data in list(pending.items()):
                    if tc_data.get("suppressed") or tid in suppressed_tool_ids:
                        continue
                    if tc_data.get("name") and not tc_data.get("announced"):
                        tool_iterations += 1
                        for notice in self._maybe_budget_notice(
                            tool_iterations,
                            budget_notices_emitted,
                        ):
                            for ev in _take_text(notice):
                                yield ev
                        yield (
                            "tool_call",
                            {"id": tid, "name": tc_data["name"], "args": tc_data["args"]},
                        )
                        _note_tool(tid)
                    self._append_tool_session(
                        thread_id,
                        "tool_call",
                        name=tc_data["name"],
                        content=tc_data["args"],
                    )
                pending.clear()

                if not full_text:
                    if did_resume:
                        state_text = self._extract_last_ai_text(current_state)
                        logger.debug(
                            "astream_events: post-resume full_text empty — state fallback produced %d chars",
                            len(state_text),
                        )
                        if state_text:
                            for ev in _take_text(state_text):
                                yield ev
                    else:
                        # Snapshot AFTER the stream, so the ids excluded here
                        # are both the thread's history and whatever this
                        # turn's chunks already billed above — leaving exactly
                        # what the re-invoke adds.
                        seen_ids = await self._billed_message_ids(thread_id)
                        result = await agent.ainvoke(
                            {"messages": [{"role": "user", "content": message}]},
                            config=self._run_config(thread_id),
                        )
                        self._merge_result_usage(
                            usage_meta,
                            result,
                            model=effective_config.model,
                            seen_ids=seen_ids,
                        )
                        last_finish_reason = self._extract_finish_reason(result)
                        text = self._extract_response(result)
                        if text:
                            for ev in _take_text(text):
                                yield ev

                continuation = await self._continue_truncated_response(
                    thread_id,
                    last_finish_reason,
                    usage_meta,
                    agent=agent,
                    model=effective_config.model,
                )
                for text in continuation:
                    for ev in _take_text(text):
                        yield ev

            # A reply shorter than the marker ("Hi") ends with its text still
            # held by the silence filter. Release it here, on the last path
            # that may still yield — `finally` can only record.
            if silence is not None:
                held = silence.flush()
                if held:
                    for ev in _take_text(held):
                        yield ev
                if silence.silenced:
                    yield ("message_policy", {"action": "no_reply", "source": "ignore_rules"})

            # The fallbacks above re-run the graph with no chunk to wake the
            # loop — report anything they drained.
            async for ev in _flush_steers(emit=True):
                yield ev

        except AgentError:
            async for ev in _flush_steers(emit=True):
                yield ev
            raise
        except Exception as exc:
            # Report delivered steers even on a failed turn: the dashboard
            # would otherwise re-send a message the checkpoint already holds.
            async for ev in _flush_steers(emit=True):
                yield ev
            if full_text and _is_codex_stream_tail_parse_error(exc, provider_key):
                logger.warning(
                    "Ignoring OpenAI Codex stream terminal parse error after %d chars",
                    sum(len(t) for t in full_text),
                )
                return
            raise _agent_error(exc, provider_key) from exc
        finally:
            # Steers the model read that the loop never got to report (the
            # turn ended on the very call that consumed them): rows only — a
            # generator being closed may not yield.
            async for _ in _flush_steers(emit=False):
                pass
            # Same release as above, for a turn that ended on an error or a
            # cancellation before reaching it. Rows only — a generator being
            # closed may not yield — so `_take_text`'s events are discarded and
            # only `full_text`/`segments` are updated for the persist below.
            if silence is not None:
                held = silence.flush()
                if held:
                    _take_text(held)
            remainder = "".join(full_text[flushed_upto:])
            if remainder:
                # Ordered segments ride along only when the turn actually
                # interleaved text with tool activity AND no steer split the
                # reply: a split row's segments would not add up to its own
                # content, and the dashboard falls back to the tools-first
                # layout for such a row anyway.
                extra: dict[str, Any] | None = None
                if len(segments) > 1 and flushed_upto == 0:
                    extra = {
                        "segments": [
                            {"text": str(seg["text"]), "after": seg["after"]} for seg in segments
                        ]
                    }
                await self._append_assistant_text(
                    thread_id, remainder, hook_context, extra=extra, config=effective_config
                )
                # JSONL stays append-only; deepagents handles model-context bounding.
            if full_text or tool_failures:
                self._schedule_post_turn_pulse_generation(
                    thread_id,
                    usage_meta=usage_meta,
                    tool_failures=tool_failures,
                )
            self._notify_revisions()
            if usage_meta:
                # Attribute cost to the model that actually ran (override or
                # configured) — effective_config carries the right model string.
                # The displayed cost is a pure calc, so compute it inline for an
                # exact usage frame; the heavier tracker/ledger/budget writes are
                # pushed to a background task so the prompt frees the instant the
                # last token lands instead of waiting on disk I/O.
                self._ensure_streamed_credits(usage_meta, effective_config.model)
                try:
                    from olano.analytics.threadcost.cost import cost_for_call

                    usd = cost_for_call(
                        _resolved_model(effective_config.model) or effective_config.model,
                        usage_meta.get("input_tokens", 0),
                        usage_meta.get("output_tokens", 0),
                        usage_meta.get("cache_read", 0),
                    )
                except Exception:
                    usd = 0.0
                self._schedule_usage_recording(effective_config, dict(usage_meta), thread_id)
                self._bump_thread_credits(thread_id, usage_meta)
                usage_frame: dict[str, Any] = dict(usage_meta)
                # `credits_micro` is an internal fixed-point accumulator (the
                # usage_meta dict is int-typed); the frame carries the real
                # unit deliberately instead of leaking the raw counter.
                credits_micro = int(usage_frame.pop("credits_micro", 0) or 0)
                if credits_micro:
                    usage_frame["credits"] = round(credits_micro / 1_000_000, 6)
                if usd:
                    usage_frame["usd"] = round(usd, 6)
                # The live context-window fill for the header/`/context` bar: the
                # last sub-call's prompt size, NOT the per-turn input sum. Falls back
                # to the summed input_tokens for single-call turns (where they match).
                usage_frame["context_tokens"] = last_input_tokens or usage_meta.get(
                    "input_tokens", 0
                )
                # Model sub-calls this turn — the chat header adds them into a
                # running per-thread total (see _merge_usage_metadata).
                usage_frame["model_calls"] = int(usage_meta.get("model_calls", 0) or 0)
                yield ("usage", usage_frame)
            # Automatic context bounding is owned entirely by deepagents'
            # SummarizationMiddleware (model-aware: it triggers near the model's
            # real max_input_tokens, preserves recent ToolMessages, and offloads
            # full history to conversation_history/<thread>.md). olano no
            # longer force-forks a compacted successor thread at a hardcoded
            # token count — that fired far below modern context windows and
            # dropped tool results, so the agent "forgot" what it had just done.
            # On-demand compaction remains available via the `/compact` endpoint,
            # which asks the agent to call deepagents' `compact_conversation` tool.
            if not thread_switch:
                requested_switch = hook_context.pop("thread_switch", None)
                if isinstance(requested_switch, dict):
                    thread_switch = {
                        "old_thread_id": thread_id,
                        "new_thread_id": requested_switch.get("new_thread_id", str(uuid.uuid4())),
                        "reason": requested_switch.get("reason", "fresh_thread"),
                    }
            if thread_switch:
                yield ("thread_switch", thread_switch)
            hook_context.pop("internal_message", None)
            try:
                _ca.reset(_ca_runner_token)
                _google_agent_ctx.reset(_google_ctx_token)
                _agent_env.reset(_agent_env_token)
                TURN_TOOL_FAILURES.reset(_turn_failures_token)
            except Exception:
                pass
            request_ctx.__exit__(None, None, None)
