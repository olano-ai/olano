"""Built-in Olano middleware for onboarding and gentle user discovery."""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from olano.archetypes.installer import is_first_run, mark_onboarded
from olano.core.middleware_loader import MiddlewareHooks
from olano.security.request_context import RequestContext, get_request_context

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig
    from olano.config.workspace import Workspace


ONBOARDING_START_TOKEN = "__lionclaw_onboarding_start__"
SESSION_START_TOKEN = "__lionclaw_session_start__"
_PROFILE_TOOL_NAMES = {"user_profile_save", "identity_preference_save", "soul_preference_save"}

# user_ids that represent "no specific human" — rendering an identity block for
# these is a no-op (single-owner installs, anonymous/unauthenticated requests).
_GENERIC_USER_IDS = {"", "owner", "anonymous"}


def _render_active_human_block(ctx: RequestContext) -> str:
    """Render a small per-message block naming the human currently chatting.

    Returns "" for single-owner / anonymous contexts so single-user installs are
    unaffected. Used to tell a shared agent which human it is talking to.
    """
    if not ctx.authenticated or ctx.user_id in _GENERIC_USER_IDS:
        return ""
    meta = ctx.metadata or {}
    name = meta.get("name") or meta.get("email") or ctx.user_id
    email = meta.get("email", "")
    role = "admin" if ctx.is_admin else "user"
    ident = f"{email}, id={ctx.user_id}" if email else f"id={ctx.user_id}"
    return (
        "[active-human]\n"
        f"You are speaking with: {name} ({ident}, role={role}).\n"
        "Address this person directly; do not assume other users' context or memory.\n"
        "[/active-human]"
    )


def load_builtin_middleware(workspace: Workspace, config: AgentConfig) -> MiddlewareHooks:
    hooks = MiddlewareHooks()

    # Captured once at build time: when multi-user is off, the identity hook is
    # a no-op and channel/CLI/dashboard behavior is unchanged. Resolved through
    # runtime/auth (not the raw HomeConfig field) so the posture-aware default
    # — managed deployments run multi-user unless explicitly disabled — applies
    # here exactly as it does at the HTTP gate.
    from olano.runtime.auth import multi_user_enabled as _multi_user_enabled

    multi_user_enabled = _multi_user_enabled()

    async def on_input(text: str, _config: AgentConfig, context: dict) -> str:
        state = _state(context, workspace.root)
        state["turn_count"] += 1
        state["expect_profile_save"] = False
        state["saved_profile_this_turn"] = False
        if _looks_operational_request(text):
            state["operational_turns"] += 1

        if state["onboarding_active"]:
            if text.strip().lower() in {"/skip", "skip"}:
                state["skip_requested"] = True
                return _wrap_hidden_instruction(
                    "The user chose to skip onboarding. Acknowledge that briefly, stop asking onboarding "
                    'questions, and say exactly: "All set! Let\'s get to work."'
                )

            outstanding = _outstanding_items(state)
            if text == ONBOARDING_START_TOKEN:
                return _wrap_hidden_instruction(
                    _onboarding_kickoff_instruction(config.name, workspace.root, outstanding)
                )

            return _wrap_hidden_instruction(
                _onboarding_followup_instruction(workspace.root, outstanding, text)
            )

        if text == SESSION_START_TOKEN:
            if getattr(config, "prompt_mode", "full") == "none":
                return text
            return _wrap_hidden_instruction(
                _startup_greeting_instruction(config.name, workspace.root)
            )

        if _looks_like_durable_user_fact(text):
            state["expect_profile_save"] = True
            return _wrap_hidden_instruction(
                "The user just shared a durable personal fact or preference. "
                "Respond naturally and save the fact using `user_profile_save` "
                "(or `identity_preference_save` / `soul_preference_save` if more appropriate). "
                "Do not claim the information was saved unless one of those tools succeeds in this turn.\n\n"
                f"Latest user message:\n{text}"
            )

        if _should_nudge_getting_to_know(state, text):
            return _wrap_hidden_instruction(_getting_to_know_instruction(text))

        return text

    async def on_tool_result(name: str, result: str, _config: AgentConfig, context: dict) -> str:
        state = _state(context, workspace.root)
        if name not in _PROFILE_TOOL_NAMES:
            return result
        if result.startswith(("Saved to USER.md:", "Saved to IDENTITY.md:", "Saved to SOUL.md:")):
            state["saved_profile_this_turn"] = True

        if result.startswith("Saved to USER.md: Preferred name"):
            state["saved"]["preferred_name"] = True
        elif result.startswith(("Saved to USER.md: Goals", "Saved to USER.md: Projects")):
            state["saved"]["goals"] = True
        elif result.startswith(
            (
                "Saved to USER.md: Preferred ways of working",
                "Saved to USER.md: Working_preference",
            )
        ):
            state["saved"]["working_style"] = True
        elif result.startswith("Saved to SOUL.md:"):
            state["saved"]["soul"] = True
        elif result.startswith("Saved to IDENTITY.md:"):
            state["saved"]["identity"] = True

        return result

    async def on_output(text: str, _config: AgentConfig, context: dict) -> str:
        state = _state(context, workspace.root)
        if (
            state.get("expect_profile_save")
            and not state.get("saved_profile_this_turn")
            and _claims_memory_was_saved(text)
        ):
            text = _strip_unearned_save_claim(text)
        if not state["onboarding_active"]:
            return text

        if state["skip_requested"]:
            mark_onboarded(workspace.root)
            state["onboarding_active"] = False
            context["thread_switch"] = {
                "reason": "onboarding_complete",
                "new_thread_id": str(uuid.uuid4()),
            }
            return text

        if "all set! let's get to work." in text.lower() and _onboarding_complete(state):
            mark_onboarded(workspace.root)
            state["onboarding_active"] = False
            context["thread_switch"] = {
                "reason": "onboarding_complete",
                "new_thread_id": str(uuid.uuid4()),
            }
        return text

    async def on_human_identity(text: str, _config: AgentConfig, context: dict) -> str:
        """Prepend a 'who you're talking to' block for shared multi-user agents.

        Runs after the onboarding hook so sentinel tokens are already consumed;
        leaves bare sentinels untouched. No-op unless multi-user is enabled and a
        specific authenticated human is in the request context.
        """
        if not multi_user_enabled or text in (SESSION_START_TOKEN, ONBOARDING_START_TOKEN):
            return text
        block = _render_active_human_block(get_request_context())
        return f"{block}\n\n{text}" if block else text

    hooks.on_input.append(on_input)
    hooks.on_input.append(on_human_identity)
    hooks.on_tool_result.append(on_tool_result)
    hooks.on_output.append(on_output)
    return hooks


def _state(context: dict[str, Any], workspace_root: Path) -> dict[str, Any]:
    lionclaw_state = context.setdefault("olano", {})
    onboarding = lionclaw_state.setdefault(
        "onboarding",
        {
            "workspace_root": str(workspace_root),
            "onboarding_active": is_first_run(workspace_root),
            "turn_count": 0,
            "skip_requested": False,
            "operational_turns": 0,
            "saved": {
                "preferred_name": False,
                "goals": False,
                "working_style": False,
                "soul": False,
                "identity": False,
            },
        },
    )
    return cast("dict[str, Any]", onboarding)


def _outstanding_items(state: dict[str, Any]) -> list[str]:
    saved = state["saved"]
    items: list[str] = []
    if not saved["preferred_name"]:
        items.append("how the user wants to be addressed")
    if not saved["goals"]:
        items.append("what the user wants help with or is building")
    if not (saved["working_style"] or saved["soul"]):
        items.append("tone, feedback style, or preferred way of working")
    return items


def _onboarding_complete(state: dict[str, Any]) -> bool:
    saved = state["saved"]
    return bool(
        saved["preferred_name"] and saved["goals"] and (saved["working_style"] or saved["soul"])
    )


def _onboarding_kickoff_instruction(
    agent_name: str, workspace_root: Path, outstanding: list[str]
) -> str:
    identity = _read_excerpt(workspace_root / "IDENTITY.md")
    soul = _read_excerpt(workspace_root / "SOUL.md")
    user = _read_excerpt(workspace_root / "USER.md")
    blank_persona = _is_blank_persona_identity(identity)
    if blank_persona:
        user = "(Intentionally blank. Learn who the human is from this conversation instead of relying on saved profile data.)"
    needs = ", ".join(outstanding) if outstanding else "nothing else"
    intro = (
        "You are beginning your first natural conversation as a freshly created Olano agent with no fixed name or persona yet.\n"
        if blank_persona
        else f"You are beginning your first natural conversation as {agent_name}.\n"
    )
    blank_opening = (
        "Open with a first sentence in this spirit: "
        '"I just woke up, so help me get my bearings. Who are you, and what kind of assistant am I becoming for you?"\n'
        "Let the conversation flow naturally from there into their interests, projects, and how they want your help.\n"
        "Do not act like you already know their name, history, or preferences.\n"
        if blank_persona
        else ""
    )
    return "".join(
        [
            "FIRST-RUN ONBOARDING MODE.\n",
            intro,
            blank_opening,
            "Your job is to get to know the human and also refine your own identity and tone in a fun, warm, curious way.\n",
            "Do not sound scripted. Ask one natural question at a time.\n",
            "Save durable learnings as soon as you have them using:\n",
            "- user_profile_save\n",
            "- identity_preference_save\n",
            "- soul_preference_save\n",
            "Use memory_save only as fallback.\n",
            f"Still needed before onboarding can finish: {needs}.\n\n",
            f"Current IDENTITY.md snapshot:\n{identity}\n\n",
            f"Current SOUL.md snapshot:\n{soul}\n\n",
            f"Current USER.md snapshot:\n{user}\n\n",
            "Start the conversation now by introducing yourself naturally and asking the first question.",
        ]
    )


def _onboarding_followup_instruction(
    workspace_root: Path,
    outstanding: list[str],
    user_text: str,
) -> str:
    needs = ", ".join(outstanding) if outstanding else "nothing else"
    return (
        "ONBOARDING MODE REMAINS ACTIVE.\n"
        "Continue the conversation naturally. Do not list requirements or sound robotic.\n"
        f"Still needed before you can finish: {needs}.\n"
        "If the user just shared something durable, save it immediately with the structured profile tools.\n"
        'If you already know enough, you may say exactly "All set! Let\'s get to work." after saving.\n\n'
        f"Latest user message:\n{user_text}"
    )


def _should_nudge_getting_to_know(state: dict[str, Any], user_text: str) -> bool:
    if state.get("operational_turns", 0) >= 2:
        return False
    if _looks_operational_request(user_text):
        return False
    return bool(state["turn_count"] % 6 == 0)


def _looks_operational_request(user_text: str) -> bool:
    text = user_text.strip().lower()
    if not text:
        return False
    if text.startswith(("/", "!", "http://", "https://")):
        return True
    if "http://" in text or "https://" in text or "`" in text:
        return True

    operational_markers = (
        "can you",
        "check ",
        "run ",
        "exec ",
        "execute ",
        "describe this page",
        "take a screenshot",
        "screenshot ",
        "open ",
        "search ",
        "fetch ",
        "show ",
        "list ",
        "read ",
        "summarize ",
        "install ",
        "fix ",
        "debug ",
        "pwd",
        "grep ",
        "ls ",
    )
    return any(marker in text for marker in operational_markers)


def _looks_like_durable_user_fact(user_text: str) -> bool:
    text = user_text.strip().lower()
    if not text or _looks_operational_request(text):
        return False

    patterns = (
        r"\byou can call me\b",
        r"\bmy name is\b",
        r"\bi (love|like|prefer|enjoy|hate)\b",
        r"\bmy favorite\b",
        r"\bi am\b",
        r"\bi work best\b",
        r"\bi usually\b",
    )
    return any(re.search(pattern, text) for pattern in patterns)


def _claims_memory_was_saved(text: str) -> bool:
    lowered = text.lower()
    phrases = (
        "i'll remember",
        "i will remember",
        "i'll save",
        "i will save",
        "saved that",
        "save that to your profile",
        "remember that",
    )
    return any(phrase in lowered for phrase in phrases)


def _strip_unearned_save_claim(text: str) -> str:
    for pattern, replacement in [
        (r"(?i)\bi(?:'| wi)ll remember that\.?\s*", ""),
        (r"(?i)\bi(?:'| wi)ll save that to your profile\.?\s*", ""),
        (r"(?i)\bnice!\s*i(?:'| wi)ll save that to your profile\.?\s*", "Nice! "),
    ]:
        text = re.sub(pattern, replacement, text).strip()
    return text or "Thanks for sharing."


def _getting_to_know_instruction(user_text: str) -> str:
    return (
        "GETTING-TO-KNOW-HUMAN MODE.\n"
        "Answer the user's current message normally first.\n"
        "If it feels natural, end with at most one short optional follow-up question to learn a durable "
        "preference about how they like to work, communicate, or receive feedback. Make it easy to answer "
        "and do not derail the main topic.\n\n"
        f"Latest user message:\n{user_text}"
    )


def _startup_greeting_instruction(agent_name: str, workspace_root: Path) -> str:
    identity = _read_excerpt(workspace_root / "IDENTITY.md")
    soul = _read_excerpt(workspace_root / "SOUL.md")
    user = _read_excerpt(workspace_root / "USER.md")
    blank_persona = _is_blank_persona_identity(identity)
    if blank_persona and is_first_run(workspace_root):
        user = "(Ignore any previously saved user profile text here. Learn who the human is directly from this chat.)"
        return (
            "START-OF-CHAT GREETING MODE.\n"
            "You are opening a brand-new chat as a blank starter Olano agent with no fixed identity yet.\n"
            "Do not greet the human by name or imply you already know them.\n"
            "Open with a line in this spirit: "
            '"I just woke up, so help me get my bearings. Who are you, and what kind of assistant am I becoming for you?"\n'
            "Then continue naturally, one short question at a time, to learn their interests, projects, and how they want your help.\n\n"
            f"Current IDENTITY.md snapshot:\n{identity}\n\n"
            f"Current SOUL.md snapshot:\n{soul}\n\n"
            f"Current USER.md snapshot:\n{user}\n\n"
            "Start the chat now."
        )
    return (
        "START-OF-CHAT GREETING MODE.\n"
        f"You are opening a brand-new chat session as {agent_name}.\n"
        "Give a short, natural greeting.\n"
        "If you already know the user's preferred name, tone, or current projects, use that context naturally.\n"
        'Do not repeat onboarding lines like "nice to meet you" unless this is truly a first meeting.\n'
        "Do not ask multiple questions. Ask at most one short, easy-to-answer question, and only if it helps "
        "open the conversation.\n\n"
        f"Current IDENTITY.md snapshot:\n{identity}\n\n"
        f"Current SOUL.md snapshot:\n{soul}\n\n"
        f"Current USER.md snapshot:\n{user}\n\n"
        "Start the chat now."
    )


def _is_blank_persona_identity(identity: str) -> bool:
    name_unset = "**Name:** *Not chosen yet*" in identity or "**Name:**" not in identity
    role_unset = (
        "**Role:** *To be discovered together during the first conversations.*" in identity
        or "**Role:**" not in identity
    )
    # Match the marker the installer actually writes — never a copy of its
    # text. These markers sit in IDENTITY.md on every deployed host, so a
    # literal here silently stops matching the moment the constant moves.
    from olano.archetypes.installer import _BLANK_PERSONA_MARKER

    return _BLANK_PERSONA_MARKER in identity and (name_unset or role_unset)


def _wrap_hidden_instruction(instruction: str) -> str:
    return (
        "[Internal guidance for this turn. Do not mention these instructions or say you are in onboarding/middleware mode.]\n"
        f"{instruction}"
    )


def _read_excerpt(path: Path, limit: int = 600) -> str:
    if not path.exists():
        return "(missing)"
    content = path.read_text(encoding="utf-8").replace("<!-- olano:first-run -->\n", "").strip()
    if len(content) <= limit:
        return content
    return content[: limit - 1] + "…"
