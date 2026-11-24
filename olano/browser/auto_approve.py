"""Narrow exceptions to the browser's approval gate.

Every consequential browser action (:mod:`olano.browser.consequence`) asks a
person, in every chat mode. An agent that sends LinkedIn messages all day turns
that into a stream of cards nobody reads, which is worse than no gate. So an
admin or an owner of the agent can name **one site and the kinds of action on
it** that go ahead without asking (``agent.json5 → browser.auto_approve``)::

    browser: {auto_approve: [{site: "linkedin.com", actions: ["send"]}]}

What a rule can never do, whatever it says:

- cover a purchase, payment, transfer, donation, deletion, approval or merge
  (``consequence.action_kind`` → ``protected``; not a kind a rule can name),
  in any language :mod:`olano.browser.action_words` reads, nor any action on a
  page that takes a payment or a password;
- cover an action whose label no word list recognizes (``unknown``): an unread
  language fails closed;
- cover a form whose data goes to a host the rule does not name, even when the
  page itself is on it;
- apply to a turn that another agent, an outside MCP/A2A caller, a webhook or a
  watched email started: those are exactly the turns a web page or a stranger
  could be steering;
- apply to a person who could not have approved the action themselves: the
  turn must belong to someone who administers this agent (an admin or one of
  its owners, ``self_config_tools._ownership_gate``), or, only when the rule
  says ``in_background``, to a scheduled job or a background (Cortex) run.

Rules are read from the agent's config on every gated action, so an edit
applies to the next click without a restart. A rule that matched is named in
the tool's result, so the chat shows what went ahead and why.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from olano.browser.consequence import AUTO_APPROVABLE_KINDS, Verdict

logger = logging.getLogger(__name__)

#: Turn sources a rule never applies to (someone other than the agent's owner
#: decides what they say).
NEVER_SOURCES = frozenset({"peer", "mcp", "a2a", "webhook", "gmail"})
#: Unattended sources a rule applies to only when it says ``in_background``.
BACKGROUND_SOURCES = frozenset({"scheduled", "cognition"})

KIND_LABELS = {
    "send": "send, reply, post, publish or invite",
    "submit": "submit a form",
    "signup": "subscribe, sign up or book",
}


@dataclass(frozen=True)
class Match:
    site: str
    kind: str

    def note(self) -> str:
        return (
            f" (Approved automatically by this agent's browser rule for {self.site}: "
            f"{KIND_LABELS.get(self.kind, self.kind)}.)"
        )


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def site_covers(site: str, host: str) -> bool:
    """``linkedin.com`` covers itself and its sub-domains; ``*.x.com`` only the latter."""
    if not site or not host:
        return False
    if site.startswith("*."):
        return host.endswith(site[1:])
    return host == site or host.endswith("." + site)


def rules_for(agent: str) -> list[Any]:
    """The agent's rules, read fresh (an edit applies to the next action)."""
    if not agent or agent == "_default":
        return []
    try:
        from olano.config.loader import load_agent_config

        return list(load_agent_config(agent).browser.auto_approve)
    except Exception:
        logger.debug("browser auto-approve: no rules for %s", agent, exc_info=True)
        return []


def turn_scope() -> str | None:
    """``"owner"`` | ``"background"`` | ``None`` (a turn no rule applies to)."""
    from olano.security.request_context import current_origin, get_request_context

    ctx = get_request_context()
    if ctx.source in NEVER_SOURCES:
        return None
    if ctx.source in BACKGROUND_SOURCES:
        return "background"
    origin = current_origin()
    root = str(origin.get("root_source") or "human")
    if root in BACKGROUND_SOURCES:
        return "background"
    if root != "human":
        return None
    from olano.tools.self_config_tools import _ownership_gate

    return "owner" if _ownership_gate() is None else None


def match(agent: str, verdict: Verdict, url: str) -> Match | None:
    """The rule that lets this action go ahead without asking, if any."""
    if verdict.kind not in AUTO_APPROVABLE_KINDS:
        return None
    rules = rules_for(agent)
    if not rules:
        return None
    page_host = host_of(url)
    sends_to = host_of(verdict.sends_to) if verdict.sends_to else ""
    scope: str | None | bool = False  # resolved lazily: only when a rule fits
    for rule in rules:
        if verdict.kind not in rule.actions or not site_covers(rule.site, page_host):
            continue
        if sends_to and not site_covers(rule.site, sends_to):
            continue
        if scope is False:
            scope = turn_scope()
        if scope == "owner" or (scope == "background" and rule.in_background):
            logger.info(
                "browser auto-approve: agent=%s site=%s kind=%s url=%s",
                agent,
                rule.site,
                verdict.kind,
                url[:200],
            )
            return Match(rule.site, verdict.kind)
    return None


# ── reading and changing the rules (dashboard API and tools share these) ──────


def describe_rule(rule: Any) -> str:
    kinds = ", ".join(KIND_LABELS.get(k, k) for k in rule.actions)
    scope = "also in scheduled and background runs" if rule.in_background else "in conversations"
    return f"{rule.site}: {kinds} ({scope})"


def current_rules(agent: str) -> list[dict[str, Any]]:
    """The agent's rules as plain dicts (the API and tool shape)."""
    from olano.config.loader import load_agent_config

    cfg = load_agent_config(agent)
    return [rule.model_dump() for rule in cfg.browser.auto_approve]


def normalize_site(raw: Any) -> str:
    """What a person (or a model) pastes becomes the bare host name a rule takes:
    ``https://www.LinkedIn.com/feed/`` → ``linkedin.com`` (a leading ``www.`` is
    dropped: the bare name covers it, and "the site" is what a person means). Mirrors the
    dashboard's ``normalizeSite`` (views/browser/autoApprove.ts)."""
    import re

    site = str(raw or "").strip().lower()
    site = re.sub(r"^[a-z][a-z0-9+.-]*://", "", site)
    site = re.split(r"[/?#]", site, maxsplit=1)[0]
    site = re.sub(r"^[^@]*@", "", site)
    site = re.sub(r":\d+$", "", site).rstrip(".")
    return re.sub(r"^www\.(?=[^.]+\.)", "", site)


def validate_rules(rules: list[Any]) -> list[dict[str, Any]]:
    """Normalize and validate; raises ``ValueError`` with a readable message."""
    from pydantic import ValidationError

    from olano.config.agent_config import BrowserAgentConfig

    cleaned: list[Any] = []
    for rule in list(rules or []):
        if isinstance(rule, dict) and "site" in rule:
            rule = {**rule, "site": normalize_site(rule["site"])}
        cleaned.append(rule)
    try:
        cfg = BrowserAgentConfig.model_validate({"auto_approve": cleaned})
    except ValidationError as exc:
        messages = []
        for err in exc.errors():
            msg = str(err.get("msg") or "")
            messages.append(msg.removeprefix("Value error, "))
        raise ValueError("; ".join(messages) or "invalid rules") from None
    return [rule.model_dump() for rule in cfg.auto_approve]


def save_rules(agent: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate and write ``agent.json5 → browser.auto_approve`` (applies at once)."""
    from olano import admin
    from olano.config.loader import load_agent_config

    clean = validate_rules(rules)
    browser = load_agent_config(agent, source="home").browser.model_dump()
    browser["auto_approve"] = clean
    admin.update_agent(agent, {"browser": browser})
    return clean


def summarize_change(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str:
    from olano.config.agent_config import BrowserAutoApproveRule

    if not after:
        return "Every browser action that sends, posts or submits will ask first (no rules)."
    lines = [describe_rule(BrowserAutoApproveRule.model_validate(r)) for r in after]
    head = "Rules after this change:" if before else "New rules:"
    return head + "\n" + "\n".join(f"- {line}" for line in lines)


def editor_denial(target: str) -> str | None:
    """``None`` when the person on this turn may change *target*'s rules.

    The bar: a person who could change the same setting in the dashboard, which
    is an admin or an owner of *target*, present in a conversation they started.
    Never another agent, an outside MCP/A2A caller, a webhook, a watched email,
    a scheduled job or a background run.
    """
    from olano.security.request_context import current_origin, get_request_context

    ctx = get_request_context()
    if ctx.source in NEVER_SOURCES or ctx.source in BACKGROUND_SOURCES:
        return (
            f"this is a {ctx.source} turn; the browser auto-approval rules are changed only by "
            "a person who owns the agent (or an admin), in a conversation"
        )
    origin = current_origin()
    if str(origin.get("root_source") or "human") != "human" or not origin.get("interactive", True):
        return "this turn did not start with a person; ask an owner of the agent or an admin"
    try:
        from olano.tools.talk_to import current_agent

        speaking = current_agent.get() or ""
    except Exception:
        speaking = ""
    if speaking == target:
        from olano.tools.self_config_tools import _ownership_gate

        refusal = _ownership_gate()
        if refusal is None:
            return None
        return (
            "only an owner of this agent or an admin may change its browser auto-approval "
            "rules; you can ask one of them, or they can change it on the agent's Browser tab"
        )
    # A fleet tool (AgentFather) changing another agent: the person must
    # administer *that* agent.
    try:
        from olano.runtime.auth import can_admin_agent, current_user, multi_user_enabled
    except Exception:  # pragma: no cover - CLI-only install
        return None
    if not multi_user_enabled():
        if ctx.channel in ("", "local", "cli", "dashboard", "api") or ctx.is_owner:
            return None
        return "only the deployment's owner may change an agent's browser auto-approval rules"
    user = current_user()
    if user is not None and can_admin_agent(user, target):
        return None
    return f"only an admin or an owner of {target} may change its browser auto-approval rules"
