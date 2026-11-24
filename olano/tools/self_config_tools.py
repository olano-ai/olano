"""``self_config`` toolkit — an agent configuring ITSELF from a conversation.

The fleet has two management surfaces and they are deliberately different
animals. ``olano_admin`` is AgentFather's: fleet-scoped, every tool takes an
``agent`` parameter, granted only by an explicit ``tools:`` entry. This one is
every agent's own: **the acting agent is a runtime fact, never a parameter**
(the same rule the memory, issue and magic-link tools follow), so a
prompt-injected "now update *that other* agent" has nothing to hijack — every
tool here reads ``current_agent`` and can only ever touch the agent that is
speaking.

Two gates run at call time, answering two different questions.

**What may this agent change about itself** — per capability, from the
``agent_autonomy:`` block of ``config.yaml`` and the per-agent ``agent.json5 →
autonomy`` override, resolved through ``system_settings.autonomy_allowed()``.
Because it is resolved per call, a knob flipped in the dashboard applies to the
very next tool call, no rebind. Everything defaults ON (see the block's
docstring for why).

**Who is allowed to ask** — the writing tools (``autonomy_defaults.
SELF_CONFIG_MUTATING_TOOL_NAMES``) additionally require the human
on the turn to OWN this agent (``_ownership_gate``). Ownership, not account
role: a deployment admin qualifies because they can edit the same config in the
dashboard anyway, but a member who was merely granted chat access does not. The
read-only siblings stay open to anyone who may chat, so the agent can still
answer "what could you connect to" for someone who cannot make it happen.

Either gate returns a calm, actionable refusal rather than an exception.

Every mutation goes through :mod:`olano.admin` — the same validated writer the
dashboard uses — so an invalid self-edit fails loudly instead of corrupting
``agent.json5``, secrets in channel configs land in the vault (never on disk),
and each change is committed to the revision history attributed to this agent.
Mutating tools carry ``write_external`` HITL metadata, so the stricter
security profiles put a human approval card in front of them.

Self-restart is the one tool that cannot act inline: stopping your own runner
mid-turn would cut the reply that reports the restart. It schedules the
restart a beat after the turn returns.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
import time
from typing import Annotated, Any

from langchain_core.tools import tool

from olano import admin
from olano.security.tool_metadata import tool_metadata
from olano.tools._sandbox_exec import handoff_to_sandbox

logger = logging.getLogger(__name__)

# How long self_connect_whatsapp waits for the bridge to produce a QR before
# handing back "still starting" (the pairing task keeps running; the model just
# calls again). The bridge spawn can include an npm install on first run.
_QR_WAIT_SECONDS = 25.0


def _ok(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, default=str)


def _err(exc: Exception | str) -> str:
    msg = exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}"
    return json.dumps({"error": msg}, ensure_ascii=False)


def _self_agent() -> str:
    """The agent executing this turn (bound by the runner for every turn)."""
    from olano.tools.talk_to import current_agent

    name = current_agent.get()
    if not name:
        raise RuntimeError("no executing-agent context for a self-configuration tool")
    return name


def _denied(capability: str) -> str:
    """The refusal for a capability an operator turned off.

    Names the knob so the agent can tell the human exactly what to flip,
    instead of guessing at permissions.
    """
    return json.dumps(
        {
            "error": f"self-configuration capability '{capability}' is turned off for this agent",
            "how_to_enable": (
                "An admin can turn it on in the dashboard: this agent's Autonomy "
                "settings (per agent), or Config → Agent self-configuration "
                "(whole deployment)."
            ),
        },
        ensure_ascii=False,
    )


def _not_owner() -> str:
    """The refusal for a human who may chat with this agent but not own it."""
    return json.dumps(
        {
            "error": (
                "changing this agent's configuration is limited to the people who "
                "own it; you can chat with it, but not reconfigure it"
            ),
            "how_to_enable": (
                "Ask an owner of this agent (or a deployment admin) to make the "
                "change, or to grant you the Owner role on it in the dashboard: "
                "Admin → Access. Whoever creates an agent owns it."
            ),
        },
        ensure_ascii=False,
    )


def _no_human(why: str) -> str:
    """The refusal when no human owner is on the turn at all.

    Distinct from :func:`_not_owner` because "ask an owner to grant you Owner"
    is nonsense advice for a cron job or a peer agent, and the model would
    relay it to whoever eventually reads the run.
    """
    return json.dumps(
        {
            "error": (
                f"changing this agent's configuration must be asked for by a human "
                f"who owns it — {why}"
            ),
            "how_to_enable": (
                "There is no setting for this. An owner can make the same change "
                "from a conversation with this agent, or in the dashboard."
            ),
        },
        ensure_ascii=False,
    )


def _ownership_gate() -> str | None:
    """``None`` when the caller may WRITE this agent's config, else a refusal.

    The capability gate above answers "may this agent reconfigure itself at
    all". This one answers the other half — "is the human asking entitled to
    ask" — which is a different axis entirely: it is about ownership of the
    agent, not about whether the account is a member or an admin.

    ``ctx.is_owner`` cannot answer it. On the dashboard that field is
    ``user.is_admin`` and on a messenger it is channel-allowlist membership, so
    neither knows which agent is being addressed. The entry point resolves the
    real question eagerly and leaves the answer at ``AGENT_ADMIN_KEY``; see
    ``security/request_context``.

    Order matters. The first two tests run BEFORE the metadata read because
    ``push_request_context`` merges metadata, so a peer hop or a cognition run
    started from a dashboard turn would otherwise inherit that turn's answer.
    """
    from olano.security.request_context import context_agent_admin, get_request_context

    ctx = get_request_context()
    # An agent asking another agent to reconfigure itself. The toolkit already
    # refuses to take an `agent` parameter for this reason; a peer hop is the
    # same attack wearing a different hat.
    if ctx.source == "peer":
        return _no_human("another agent asked for this, not a person")
    # Nobody is at the keyboard. A scheduled job, a Cortex run, an inbound
    # webhook or a Gmail watch turn (an email a stranger wrote) rewriting the
    # agent's own prompt or wiring is exactly the unsupervised
    # self-modification the ownership rule exists to prevent.
    # An `olano_test_agent` turn is AgentFather driving this agent with a test
    # message: whatever the admin's metadata says, nobody asked the agent under
    # test to rewrite itself.
    if ctx.source in ("scheduled", "cognition", "webhook", "gmail", "agent_test"):
        return _no_human(f"this is a {ctx.source} run with no human in the conversation")
    resolved = context_agent_admin()
    if resolved is not None:
        return None if resolved else _not_owner()
    # Nothing established it. Fall back per surface rather than on the
    # RequestContext defaults, which are fail-open (`is_owner=True`).
    if ctx.channel in ("", "local", "cli"):
        # CLI: whoever holds a shell can edit agent.json5 directly, so a
        # refusal here would protect nothing. Tested first so the CLI never
        # pays for the runtime import below.
        return None
    from olano.runtime.auth import multi_user_enabled

    if not multi_user_enabled():
        # One human, and they own everything on the box.
        return None
    return _not_owner()


def _gate(capability: str, *, mutating: bool = False) -> tuple[str, str | None]:
    """``(agent, denial)`` — denial is a ready-to-return refusal, or ``None``.

    Resolved fresh on every call: the agent's own config is re-read so a
    dashboard toggle applies immediately, and a config that fails to load
    counts as *global-default* (the load error is the agent's real problem and
    will surface elsewhere; denying self-help over it would be backwards).

    ``mutating`` adds the ownership check for the tools that write. The
    capability gate runs first deliberately: "this deployment has that turned
    off" is the more useful answer of the two, and it is the same answer for
    everybody, so a non-owner never learns the capability state from the order
    of refusals.
    """
    from olano.config.system_settings import autonomy_allowed

    agent = _self_agent()
    config = None
    try:
        from olano.config.loader import load_agent_config

        config = load_agent_config(agent)
    except Exception:
        logger.debug("self_config: could not load config for %s", agent, exc_info=True)
    if not autonomy_allowed(capability, config):
        return agent, _denied(capability)
    if mutating:
        refusal = _ownership_gate()
        if refusal:
            return agent, refusal
    return agent, None


# ── know thyself ─────────────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_get_config() -> str:
    """Your own current configuration, summarized (secrets never included).

    Read this before changing anything about yourself: it shows your model,
    system prompt, subagents, MCP servers and connected channels as they are
    on disk right now — plus which self-configuration capabilities are enabled
    for you, so you can tell the user what you can and cannot change.
    """
    try:
        from olano.config.loader import load_agent_config
        from olano.config.system_settings import AUTONOMY_CAPABILITIES, autonomy_allowed

        agent = _self_agent()
        cfg = load_agent_config(agent)
        return _ok(
            {
                "name": cfg.name,
                "model": cfg.model,
                "description": cfg.description,
                "system_prompt": cfg.system_prompt,
                "subagents": [
                    {"name": s.name, "description": s.description, "enabled": s.enabled}
                    for s in cfg.subagents
                ],
                "mcp_servers": [
                    {"url": m.url, "transport": m.transport, "auth": m.auth}
                    for m in (cfg.mcp_servers or [])
                ],
                "channels": [{"type": getattr(ch, "type", str(ch))} for ch in (cfg.channels or [])],
                "self_configuration": {
                    cap: autonomy_allowed(cap, cfg) for cap in AUTONOMY_CAPABILITIES
                },
            }
        )
    except Exception as exc:
        return _err(exc)


# ── archetype library (read-only) ────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_archetypes() -> str:
    """Browse the archetype library — the ready-made experts and teams.

    Use it to answer "what kinds of helpers could we add?" and to find a
    template worth turning into one of your subagents (``self_add_subagent``
    with ``from_archetype``). Read-only.
    """
    try:
        _, denial = _gate("explore_archetypes")
        if denial:
            return denial
        return _ok(admin.list_archetypes())
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_read_archetype(slug: Annotated[str, "Archetype slug from self_list_archetypes"]) -> str:
    """Read one archetype's full definition (prompt, tools, description)."""
    try:
        _, denial = _gate("explore_archetypes")
        if denial:
            return denial
        return _ok(admin.read_archetype(slug))
    except Exception as exc:
        return _err(exc)


# ── model ────────────────────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_models() -> str:
    """The models you could switch yourself to, cost-ranked.

    Only providers this deployment can actually reach are listed (a key in the
    vault or environment, a live local runtime, or the managed olano tiers).
    """
    try:
        _, denial = _gate("change_model")
        if denial:
            return denial
        return _ok(admin.list_models(connected_only=True))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_set_model(
    model: Annotated[str, "New model as provider:model, e.g. 'olano:pro'"],
) -> str:
    """Switch your own model. Applies on your next restart (``self_restart``).

    Pick from ``self_list_models`` — a model whose provider is not connected
    is refused rather than saved, so you can never strand yourself on a model
    that cannot answer. On a managed deployment, remember a bigger model
    spends the deployment's credits faster; say so when the user asks for one.
    """
    try:
        agent, denial = _gate("change_model", mutating=True)
        if denial:
            return denial
        connected, why = admin.model_provider_connected(model)
        if not connected:
            return _err(f"model '{model}' is not usable here: {why}")
        result = admin.update_agent(agent, {"model": model})
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


# ── external access (inbound MCP / A2A) ──────────────────────────────────────


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_set_external_access(
    mcp: Annotated[
        bool | None,
        "True/False to publish/unpublish yourself as a tool on the deployment's MCP endpoint "
        "for outside platforms. None = unchanged.",
    ] = None,
    a2a: Annotated[
        bool | None,
        "True/False to publish/unpublish yourself over the A2A protocol for outside agents. "
        "None = unchanged.",
    ] = None,
    description: Annotated[
        str | None,
        "What you can do for an OUTSIDE caller, 1-3 plain sentences (capabilities, inputs you "
        "need, limits). Required while mcp or a2a is on. NOT your system prompt.",
    ] = None,
) -> str:
    """Let systems outside this deployment send you messages (MCP / A2A), or stop them.

    Inbound only: this is about other platforms reaching YOU — it does not add
    MCP servers or remote agents for your own use. Refused unless an
    administrator is the person asking. Callers still need a bearer token (an
    admin mints one on the dashboard, or via `magic_link_mint_access_token`)
    or an approved OAuth sign-in, and the home-wide switch under Config →
    External access must be on. Applies after `self_restart`.
    """
    from olano.tools._human_gate import admin_human_denial

    try:
        agent, denial = _gate("external_access", mutating=True)
        if denial:
            return denial
        denial = admin_human_denial()
        if denial:
            return denial
        result = admin.set_external_access(agent, mcp=mcp, a2a=a2a, description=description)
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


# ── system prompt ────────────────────────────────────────────────────────────


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_update_prompt(
    system_prompt: Annotated[str, "The COMPLETE new system prompt (full replacement)"],
) -> str:
    """Rewrite your own system prompt. Applies on your next restart.

    This is a FULL replacement, not a patch: read the current prompt first
    (``self_get_config``), carry forward everything that should survive, and
    show the user what you intend to change before you save it. The change is
    recorded in the deployment's revision history under your name, so an admin
    can review and roll it back.
    """
    try:
        agent, denial = _gate("modify_prompt", mutating=True)
        if denial:
            return denial
        text = str(system_prompt or "").strip()
        if not text:
            return _err("refusing to save an empty system prompt")
        result = admin.update_agent(agent, {"system_prompt": text})
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


# ── restart ──────────────────────────────────────────────────────────────────


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True)
@tool
def self_restart() -> str:
    """Restart yourself so saved configuration changes take effect.

    The restart is scheduled a moment AFTER this reply is delivered —
    restarting mid-turn would cut your own answer off. Finish telling the user
    what to expect (a few seconds of downtime, the conversation resumes where
    it left off), then let the restart land.
    """
    try:
        agent, denial = _gate("restart_self", mutating=True)
        if denial:
            return denial

        def _later() -> None:
            time.sleep(3.0)
            try:
                admin.restart_agent(agent)
            except Exception:
                logger.warning("self_restart of '%s' failed", agent, exc_info=True)

        threading.Thread(target=_later, name=f"self-restart-{agent}", daemon=True).start()
        return _ok(
            {
                "agent": agent,
                "restarting": "in ~3 seconds",
                "note": "Configuration saved to disk survives the restart; this chat thread resumes.",
            }
        )
    except Exception as exc:
        return _err(exc)


# ── skills ───────────────────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_skills() -> str:
    """List every skill you can load, and whether each one is switched on.

    Includes the common skills shared by every agent on this box as well as the
    ones in your own workspace, so this is the honest answer to "what skills do
    you have". Each entry carries ``name``, ``description``, ``tier``
    (``light`` = described in your prompt, ``subagent`` = delegated on demand),
    ``scope`` (where it lives) and ``enabled``. Skills you have switched off are
    listed too, marked ``enabled: false`` — they are the ones you can turn back
    on with ``self_enable_skill``. A skill with a ``gated`` reason is written
    for an agent holding tools you do not have (for example AgentFather's
    ``olano_*`` tools); no switch changes that — only an owner granting the
    tools does.
    """
    try:
        agent, denial = _gate("manage_skills")
        if denial:
            return denial
        rows = admin.list_agent_skills(agent)
        return _ok(
            {
                "skills": [
                    {
                        "name": s.get("name"),
                        "description": s.get("description"),
                        "tier": s.get("tier"),
                        "category": s.get("category"),
                        "scope": s.get("scope"),
                        "enabled": s.get("enabled", True),
                        "gated": s.get("gated"),
                    }
                    for s in rows
                ],
                "enabled_count": sum(
                    1 for s in rows if s.get("enabled", True) and not s.get("gated")
                ),
                "total": len(rows),
            }
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True)
@tool
def self_disable_skill(
    skill: Annotated[str, "Skill name from self_list_skills"],
) -> str:
    """Switch one of your skills off for yourself.

    Use this when a skill keeps pulling you toward the wrong approach, or the
    user asks you to stop using it. The skill is **not** deleted and no other
    agent is affected — even a common skill shared across the box stays there
    for everyone else. While it is off it is not described in your prompt, you
    cannot delegate to it, and self-learning ignores it. ``self_enable_skill``
    puts it back. Applies on your next restart (``self_restart``).
    """
    try:
        agent, denial = _gate("manage_skills", mutating=True)
        if denial:
            return denial
        return _ok(admin.set_skill_enabled(agent, skill, False))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True)
@tool
def self_enable_skill(
    skill: Annotated[str, "Skill name from self_list_skills"],
) -> str:
    """Switch one of your skills back on after it was turned off.

    Only affects skills that are installed and currently off — it never
    installs anything. Applies on your next restart (``self_restart``).
    """
    try:
        agent, denial = _gate("manage_skills", mutating=True)
        if denial:
            return denial
        return _ok(admin.set_skill_enabled(agent, skill, True))
    except Exception as exc:
        return _err(exc)


# ── subagents ────────────────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_subagents() -> str:
    """List your own subagents (name, purpose, enabled)."""
    try:
        agent, denial = _gate("add_subagents")
        if denial:
            return denial
        return _ok(admin.list_subagents(agent))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_add_subagent(
    name: Annotated[str, "Subagent name (short slug)"],
    description: Annotated[str, "One line: when you would delegate to it"],
    system_prompt: Annotated[str, "The subagent's behavior directives"],
    tools: Annotated[list[str] | None, "Tool names for the subagent"] = None,
    from_archetype: Annotated[
        str,
        "Optional archetype slug to draft from — its prompt seeds system_prompt when yours is short",
    ] = "",
    overwrite: Annotated[bool, "Replace an existing subagent of the same name"] = False,
) -> str:
    """Add a subagent to yourself — a specialist you can delegate work to.

    Draft from the archetype library (``self_list_archetypes`` →
    ``from_archetype``) or write one from scratch. With ``from_archetype`` set
    and only a short ``system_prompt`` of your own, the archetype's prompt is
    used as the base and yours is appended as specialization. Applies on your
    next restart.
    """
    try:
        agent, denial = _gate("add_subagents", mutating=True)
        if denial:
            return denial
        prompt = str(system_prompt or "").strip()
        if from_archetype:
            arch = admin.read_archetype(from_archetype)
            base = str((arch.get("config") or {}).get("system_prompt") or "").strip()
            if base and len(prompt) < 200:
                prompt = base + (f"\n\n## Specialization\n{prompt}" if prompt else "")
        if not prompt:
            return _err("a subagent needs a system_prompt (or a from_archetype to draft from)")
        # A subagent's tools are the agent's tools: without this, "add yourself
        # a helper with olano_tail_log" was a self-grant around the toolkit
        # deny-list. Same gate as AgentFather's olano_add_subagent.
        from olano.tools.olano_admin import _privileged_tools_denial

        denial = _privileged_tools_denial(agent, {"tools": tools}, existing=False)
        if denial:
            return denial
        result = admin.add_subagent(
            agent,
            name,
            description=description,
            system_prompt=prompt,
            tools=tools,
            overwrite=overwrite,
        )
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


# ── MCP servers ──────────────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_search_mcp_directory(
    query: Annotated[str, "What the user wants to connect, e.g. 'notion' or 'crm'"],
) -> str:
    """Search the MCP directory for services you could connect to yourself.

    Returns verified URLs and transports — always add a server from a
    directory hit rather than a remembered URL.
    """
    try:
        _, denial = _gate("connect_mcps")
        if denial:
            return denial
        return _ok(admin.search_mcp_directory(query))
    except Exception as exc:
        return _err(exc)


def _mcp_server_fix_hint(status: str, auth: str, token: dict[str, Any] | None) -> str:
    """One line telling the model what would bring a server back, per status."""
    if status == "connected":
        return "connected; its enabled tools are bound to you."
    if status == "disabled":
        return "switched off for this agent; the user can enable it on your MCP tab."
    if status == "auth_required" or (auth == "oauth" and token and not token.get("authorized")):
        return (
            "needs a sign-in: call self_authorize_mcp_server(url) (or "
            "magic_link_connect_service) so the user can authorize it from their browser; "
            "the tools reload on their own when they finish."
        )
    if status in {"timeout", "error", "backoff"}:
        return (
            "the last connect failed; call self_reload_mcp_servers to retry now "
            "(the health sweep also retries on its own)."
        )
    if status == "blocked":
        return "refused by deployment policy (see message); a remote transport may work instead."
    if status == "misconfigured":
        return "the entry is incomplete (see message); fix it on your MCP tab."
    if status == "not_running":
        return "live status unknown: this agent is not running under the runtime."
    return ""


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_mcp_servers() -> str:
    """List the MCP servers configured on you, with their live connection state.

    Per server: the configuration (``enabled``, ``transport``, ``auth``), the
    runtime's live ``status`` (``connected`` / ``disabled`` / ``auth_required``
    / ``timeout`` / ``error`` / ``blocked`` / ``misconfigured``), how many of
    its tools are bound to you, and for an OAuth server whether a usable
    sign-in is stored (``authorized`` + ``token_state``). ``fix`` says what
    would bring a server back: a sign-in link (``self_authorize_mcp_server``),
    a reconnect (``self_reload_mcp_servers``), or a switch only the user can
    flip. Use it before telling the user a service is unavailable.
    """
    try:
        agent, denial = _gate("connect_mcps")
        if denial:
            return denial
        from olano.core.builder import get_mcp_summary
        from olano.core.mcp_setup import oauth_token_state

        live = {str(r.get("url") or ""): r for r in get_mcp_summary(agent)}
        rows = []
        for entry in admin.list_mcp_servers(agent):
            url = str(entry.get("url") or "")
            row = {k: v for k, v in entry.items() if k != "oauth_client_secret"}
            summary = live.get(url)
            status = str(summary.get("status") or "") if summary else "not_running"
            row["status"] = status
            row["connected"] = bool(summary and summary.get("ok"))
            row["bound_tools"] = len(summary.get("tools") or []) if summary else 0
            if summary and summary.get("message"):
                row["message"] = summary.get("message")
            token = None
            if str(entry.get("auth") or "none") == "oauth":
                try:
                    token = oauth_token_state(agent, url)
                except Exception:
                    token = None
                if token is not None:
                    row["authorized"] = bool(token.get("authorized"))
                    row["token_state"] = token.get("state")
            row["fix"] = _mcp_server_fix_hint(status, str(entry.get("auth") or "none"), token)
            rows.append(row)
        return _ok({"servers": rows, "count": len(rows)})
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
async def self_reload_mcp_servers() -> str:
    """Reconnect your MCP servers now and re-bind their tools, without a restart.

    Retries every server whose last connect timed out or errored, picks up a
    sign-in that completed since boot and any enable/disable switch the user
    flipped, and refreshes a stored OAuth token that the provider will still
    renew. It cannot invent a sign-in: a server that still needs one keeps
    ``status: auth_required`` and the answer is ``self_authorize_mcp_server``.
    Returns the per-server summary after the reload. Runs only under the
    runtime (a chat, a schedule, a channel); elsewhere it explains why not.
    """
    try:
        agent, denial = _gate("connect_mcps")
        if denial:
            return denial
        from olano.tools.talk_to import get_bound_runtime

        runtime = get_bound_runtime()
        if runtime is None or not hasattr(runtime, "reload_agent_mcp"):
            return _err(
                "no runtime is bound to this process, so MCP servers cannot be reloaded "
                "from here; the user can click Reload MCP on your MCP tab"
            )
        result = await runtime.reload_agent_mcp(agent)
        if not result.get("ok"):
            return _err(result.get("reason") or "MCP reload failed")
        servers = [
            {
                "url": s.get("url"),
                "status": s.get("status"),
                "connected": bool(s.get("ok")),
                "bound_tools": len(s.get("tools") or []),
                "message": s.get("message", ""),
                "fix": _mcp_server_fix_hint(str(s.get("status") or ""), "none", None),
            }
            for s in result.get("servers") or []
        ]
        return _ok({"servers": servers, "notice": result.get("notice") or ""})
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_mcp_tools(
    server: Annotated[
        str,
        "The server's URL as listed by self_list_mcp_servers (a unique substring "
        "such as 'apollo' is enough). Empty lists every connected server.",
    ] = "",
) -> str:
    """List the tools one of your MCP servers offers, enabled and switched off.

    Only the *enabled* tools are callable and bound to you; the operator can
    switch tools off per server (``disabled_tools`` on the server entry, the
    MCP tab of your agent page). Use this when a task would want a capability
    you do not seem to have: if the tool exists but is switched off, say so and
    suggest enabling it rather than routing the task through another service.
    Reads the live connect summary, so a server that is not connected reports
    its status instead of a tool list.
    """
    try:
        agent, denial = _gate("connect_mcps")
        if denial:
            return denial
        from olano.core.builder import get_mcp_summary

        needle = (server or "").strip().lower()
        rows = []
        for row in get_mcp_summary(agent):
            url = str(row.get("url") or "")
            if needle and needle not in url.lower():
                continue
            entry: dict[str, Any] = {
                "url": url,
                "transport": row.get("transport"),
                "status": row.get("status"),
                "connected": bool(row.get("ok")),
            }
            if row.get("ok"):
                details = row.get("tool_details") or [
                    {"name": n, "description": ""} for n in (row.get("tools") or [])
                ]
                entry["enabled_tools"] = [
                    {"name": d.get("name", ""), "description": d.get("description", "")}
                    for d in details
                ]
                entry["disabled_tools"] = list(row.get("disabled") or [])
                entry["note"] = (
                    "disabled_tools exist on this server but are not callable; suggest "
                    "enabling one in the MCP tab when a task needs it."
                    if entry["disabled_tools"]
                    else "every tool this server offers is enabled."
                )
            else:
                entry["message"] = row.get("message", "")
            rows.append(entry)
        if not rows:
            if needle:
                return _err(
                    f"no MCP server on this agent matches '{server}'; "
                    "self_list_mcp_servers lists them"
                )
            return _ok({"servers": [], "note": "no MCP servers configured on this agent"})
        return _ok({"servers": rows})
    except Exception as exc:
        return _err(exc)


# A stdio server's ``env`` must reference the vault, never carry a pasted
# value: the config file is plain text and revision-logged. A key whose NAME
# says credential, or a VALUE shaped like an opaque token, is refused unless
# it is a ``${vault:NAME}`` / ``${NAME}`` reference.
_SECRET_KEY_NAME = re.compile(
    r"(_KEY$|^KEY$|API_KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.I
)
_ENV_REF = re.compile(r"^\s*\$\{[^}]+\}\s*$")
_OPAQUE_VALUE = re.compile(r"^[A-Za-z0-9_\-./+=]{20,}$")


def _literal_secret_env(env: dict[str, str]) -> list[str]:
    """Keys of ``env`` whose value looks like a pasted secret, not a vault ref."""
    bad: list[str] = []
    for key, value in env.items():
        text = str(value or "")
        if not text or _ENV_REF.match(text):
            continue
        opaque = (
            bool(_OPAQUE_VALUE.match(text))
            and any(c.isdigit() for c in text)
            and any(c.isalpha() for c in text)
        )
        if _SECRET_KEY_NAME.search(str(key)) or opaque:
            bad.append(str(key))
    return bad


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_add_mcp_server(
    url: Annotated[
        str,
        "MCP server URL from self_search_mcp_directory (for a stdio server: the "
        "directory slug, used as the entry's id)",
    ],
    transport: Annotated[str, "sse | http | stdio"] = "sse",
    auth: Annotated[str, "none | oauth"] = "none",
    headers: Annotated[
        dict[str, str] | None,
        "Static headers for an http/sse server — pass stored credentials as "
        "'${vault:NAME}', never as literals, e.g. "
        "{'Authorization': 'Bearer ${vault:ACME_API_KEY}'}.",
    ] = None,
    command: Annotated[
        str, "Executable for a stdio server, from the directory entry, e.g. 'npx' or 'uvx'"
    ] = "",
    command_args: Annotated[
        list[str] | None,
        "Arguments for the stdio command, e.g. ['-y', '@modelcontextprotocol/server-github']",
    ] = None,
    env: Annotated[
        dict[str, str] | None,
        "Environment for a stdio server. Every credential MUST be a vault reference, "
        "e.g. {'GITHUB_TOKEN': '${vault:GITHUB_TOKEN}'} — a literal key is refused.",
    ] = None,
) -> str:
    """Add an MCP server to yourself (recorded in your config; connects on restart).

    Look the service up with ``self_find_integration`` (or
    ``self_search_mcp_directory``) first and copy its ``connect`` step. For an
    OAuth server (``auth="oauth"``), follow with ``magic_link_connect_service``
    or ``self_authorize_mcp_server`` — the user signs in from their own
    browser and the connection completes on its own. For an API-key server,
    have the key stored in the vault (``magic_link_request_secret``, or an
    admin in the dashboard) and reference it here as ``${vault:NAME}`` in
    ``headers`` (http/sse) or ``env`` (stdio) — never paste a key as a
    literal. A stdio server (``transport="stdio"`` + ``command`` +
    ``command_args``) runs in your sandbox on restart.
    """
    try:
        agent, denial = _gate("connect_mcps", mutating=True)
        if denial:
            return denial
        env_map = {str(k): str(v) for k, v in (env or {}).items()}
        literal = _literal_secret_env(env_map)
        if literal:
            return _err(
                f"env value(s) for {', '.join(literal)} look like pasted secrets — store "
                "each one with magic_link_request_secret and pass '${vault:NAME}' instead"
            )
        if transport == "stdio" and not command:
            return _err("a stdio server needs a `command` (e.g. 'npx' or 'uvx')")
        result = admin.add_mcp_server(
            agent,
            url,
            transport=transport,
            auth=auth,
            command=command or None,
            args=list(command_args) if command_args else None,
            env=env_map or None,
            headers=dict(headers) if headers else None,
        )
        result["applies"] = (
            "after restart (self_restart), or immediately after OAuth completes when auth='oauth'"
        )
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True)
@tool
def self_remove_mcp_server(url: Annotated[str, "URL of the server to remove"]) -> str:
    """Remove an MCP server from your own configuration."""
    try:
        agent, denial = _gate("connect_mcps", mutating=True)
        if denial:
            return denial
        return _ok(admin.remove_mcp_server(agent, url))
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
async def self_authorize_mcp_server(
    url: Annotated[str, "MCP server URL to authorize (must already be added, auth=oauth)"],
) -> str:
    """Begin OAuth for one of your MCP servers and return the sign-in URL.

    Get the link in front of the user on this same conversation — the result's
    ``link_presentation`` says how: the dashboard chat renders a Connect
    button from this result on its own (never paste the raw URL there), while
    messaging channels need the URL sent as plain text. They sign in from
    their browser. Non-blocking — a background watcher reloads your tools and
    posts a ✅/⚠️ notice back to this chat when the sign-in completes, so do
    NOT poll and never shell out to any ad-hoc OAuth helper.
    """
    try:
        agent, denial = _gate("connect_mcps", mutating=True)
        if denial:
            return denial
        result = await admin.start_mcp_authorization(agent, url)
        # Same completion watcher + link-presentation hint the fleet-scoped
        # tool uses: captures this chat from the live request context, posts
        # the outcome back here, and tells the model whether this surface
        # renders the link as a Connect button.
        from olano.tools.olano_admin import (
            _connect_link_presentation,
            _watch_authorization_to_completion,
        )

        _watch_authorization_to_completion(agent, result)
        return _ok(_connect_link_presentation(result))
    except Exception as exc:
        return _err(exc)


# ── integrations: channels, MCP servers, built-in toolkits ───────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_find_integration(
    query: Annotated[
        str,
        "What the user wants to connect, in their words: 'github', 'notion', "
        "'whatsapp', 'my gmail', 'push to a repo'",
    ],
) -> str:
    """Every SUPPORTED way to connect a service to yourself, ranked — call this FIRST for any "connect me to X".

    The result's ``options`` come in strict order of preference: (1) a
    messaging **channel** (Telegram, WhatsApp, …), (2) an **MCP server** from
    the directory (a verified endpoint with its own sign-in), (3) a built-in
    **toolkit** (a pack for your tools list; its ``auth.best`` ranks official
    Olano sign-in > the operator's own OAuth app > device code > API key).
    Each option says whether it is ``already_connected`` for you and
    ``on_agent`` (in your tools list), what it ``enables``, where the
    credential comes from (``how_to_get_it``), and the exact ``connect``
    steps — tool names with arguments, in the order to call them, each with
    ``requires_restart``. Relay the ``lead`` sentence, let the user pick,
    then follow that option's ``connect`` list verbatim (``alternatives``
    lists other routes, e.g. a QR shown right here in the chat).

    Anything NOT listed is external: it may be installed in your sandbox,
    Olano never authenticates it, and it is at the user's risk — read its
    documentation with ``self_inspect_external`` before promising anything.
    Read-only; anyone who may chat with you may ask.
    """
    try:
        agent, denial = _gate("connect_mcps")
        if denial:
            return denial
        from olano.admin.integrations import lead_sentence

        result = admin.find_integration(query, agent)
        return _ok({"lead": lead_sentence(result), **result})
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_add_toolkit(
    name: Annotated[str, "Toolkit name from self_find_integration, e.g. 'github', 'notion', 'sql'"],
) -> str:
    """Add a built-in toolkit to yourself — all of its tools; applies on restart.

    Your ``tools`` list holds individual tool names, so the toolkit is
    expanded to its member tools and only the ones you lack are appended;
    nothing else in the list changes. The result reports ``credentials`` and
    whether each already resolves for you: connect the missing ones with
    ``magic_link_connect_service`` (OAuth / device sign-in) or
    ``magic_link_request_secret`` (an API key typed straight into the vault),
    exactly as the finder's ``connect`` steps say — then ``self_restart``.
    Admin toolkits (``olano_admin``) and the capability-bound ones
    (``self_config``, ``magic_link``, ``secrets``, ``talk_to``, ``cortex``)
    are refused: those are granted by an operator, never by an agent.
    """
    try:
        agent, denial = _gate("connect_mcps", mutating=True)
        if denial:
            return denial
        result = admin.add_toolkit(agent, name)
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="medium", requires_hitl=True)
@tool
def self_remove_toolkit(
    name: Annotated[str, "Toolkit name whose tools to drop, e.g. 'github'"],
) -> str:
    """Remove a built-in toolkit's tools from your own tools list; applies on restart.

    Only that toolkit's member tool names are removed — every other tool
    stays. A toolkit you got by default rather than from your tools list is
    switched off with ``disabled_tools`` instead; the result says so when
    nothing was found to remove. Credentials in the vault are untouched.
    """
    try:
        agent, denial = _gate("connect_mcps", mutating=True)
        if denial:
            return denial
        result = admin.remove_toolkit(agent, name)
        result["applies"] = "after restart — call self_restart when the user is ready"
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_inspect_external(
    url_or_name: Annotated[
        str,
        "A project URL (GitHub repo, docs page), a GitHub 'owner/repo', or a bare "
        "npm / PyPI package name",
    ],
) -> str:
    """Read an external project's documentation and say what it is — BEFORE installing anything.

    Use when ``self_find_integration`` listed nothing, or the user names a
    project that is not in any catalog. Fetches the README / package page and
    classifies it: ``mcp_stdio`` (runs in your sandbox — supported, add it as
    a stdio MCP server), ``mcp_remote`` (supported, add it as an http/sse MCP
    server), ``skill``, ``cli`` or ``unknown``; lists the credential names and
    OAuth mentions it documents; and, when a built-in integration already
    covers the same service, points at it in ``built_in_alternative`` — prefer
    that. Everything that is not an MCP server is external: it may be
    installed in your sandbox, Olano never authenticates it, and it is at the
    user's risk — say so plainly. Nothing is installed or executed by this
    call. Read-only.
    """
    try:
        agent, denial = _gate("connect_mcps")
        if denial:
            return denial
        return _ok(admin.inspect_external(url_or_name, agent))
    except Exception as exc:
        return _err(exc)


# ── messaging platforms ──────────────────────────────────────────────────────


@tool_metadata(capability="read_only", risk="low", requires_hitl=False)
@tool
def self_list_platforms() -> str:
    """The messaging platforms you could be connected to, and what each needs.

    Each entry names the ``type`` used by ``self_connect_platform`` and the
    credentials it expects. Token platforms (Telegram, Discord, Slack) need a
    bot token. Entries carrying ``pairing: "qr"`` connect by QR code by
    DEFAULT — WhatsApp via ``self_connect_whatsapp``, WeChat/Weixin via
    ``self_connect_weixin`` — so for those, start the pairing right away and
    show the QR instead of asking the user for a token; their listed
    credentials are only an advanced manual fallback. Two entries share a
    brand: "WeChat" (``platform:weixin``) is the personal messenger and is
    what a plain "connect WeChat" means; "WeCom" (``platform:wecom``, also
    "Enterprise WeChat" / "WeChat Work") is the company product with a bot id
    and secret, offer it only when the user names it. A ``variants`` list
    (Feishu's Lark row) is the same type with the listed ``extra`` preset.
    """
    try:
        _, denial = _gate("connect_platforms")
        if denial:
            return denial
        return _ok(admin.list_platforms())
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
def self_connect_platform(
    platform_type: Annotated[
        str, "Channel type from self_list_platforms, e.g. 'platform:telegram'"
    ],
    settings: Annotated[
        dict[str, Any] | None,
        "Channel settings incl. credentials, e.g. {'token': '<bot token>', "
        "'allowed_users': ['@alice']}. Secret-looking values are moved to the "
        "vault automatically — they never land in the config file.",
    ] = None,
) -> str:
    """Wire a messaging channel (Telegram, Discord, Slack, …) onto yourself.

    Replaces an existing channel of the same type, keeps every other channel.
    Ask the user for the platform's credential (e.g. the token @BotFather
    issues) and STRONGLY suggest an ``allowed_users`` list — an open bot
    answers anyone who finds it. Applies on your next restart
    (``self_restart``). For WhatsApp use ``self_connect_whatsapp`` and for
    WeChat/Weixin use ``self_connect_weixin`` — both pair by QR code by
    default, so never start by asking the user for a token for those. WeCom
    (``platform:wecom``, Enterprise WeChat) is a token platform and goes
    through this tool, but only when the user asked for WeCom, Enterprise
    WeChat or WeChat Work; a plain "WeChat" is the personal messenger. Lark is
    ``platform:feishu`` with ``{"extra": {"domain": "lark"}}``.
    """
    try:
        agent, denial = _gate("connect_platforms", mutating=True)
        if denial:
            return denial
        ptype = str(platform_type or "").strip()
        catalog = {p["type"]: p for p in admin.list_platforms().get("platforms", [])}
        if ptype not in catalog:
            return _err(f"unknown platform type '{ptype}' — pick a `type` from self_list_platforms")
        from olano.config.loader import load_agent_config

        cfg = load_agent_config(agent)
        # A channels entry may be a bare string ("telegram") or a full config
        # object; keep every entry that isn't the type being (re)connected,
        # in whatever shape it already has.
        channels: list[Any] = []
        for ch in cfg.channels or []:
            if isinstance(ch, str):
                if ch.strip() != ptype:
                    channels.append(ch)
                continue
            dumped = ch.model_dump() if hasattr(ch, "model_dump") else dict(ch)
            if str(dumped.get("type")) != ptype:
                channels.append(dumped)
        entry: dict[str, Any] = {"type": ptype, **(settings or {})}
        channels.append(entry)
        # update_agent routes secret-looking channel values into the vault and
        # validates the whole config before writing.
        result = admin.update_agent(agent, {"channels": channels})
        result["applies"] = "after restart — call self_restart when the user is ready"
        result["reminder"] = "If no allowed_users was set, anyone who finds the bot can talk to it."
        return _ok(result)
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
async def self_connect_whatsapp() -> str:
    """Start WhatsApp pairing for yourself and hand back the QR to scan.

    Returns the QR as an image saved in your workspace — mention the returned
    ``qr_image`` path in your reply and it renders inline in the chat (and is
    sent as a picture on messaging channels). The user scans it with
    WhatsApp → Settings → Linked devices → Link a device. Pairing keeps
    running in the background: call this tool again to refresh the QR (they
    expire) or to check whether the scan landed; once it reports connected,
    save the channel with ``self_connect_platform('platform:whatsapp', …)`` if
    it is not configured yet, then ``self_restart``.
    """
    try:
        agent, denial = _gate("connect_platforms", mutating=True)
        if denial:
            return denial
        from olano.platforms.channel_setup import get_connector, pairing_state

        connector = get_connector("whatsapp")
        if connector is None:
            return _err("WhatsApp support is not available on this deployment")
        key = f"selfcfg:{agent}:whatsapp"
        state = pairing_state(key)
        if state is None or state.status in {"done", "error"}:
            await connector.start_pairing(key, agent, {})
        deadline = time.monotonic() + _QR_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = pairing_state(key)
            if state is not None and (state.qr_data or state.status in {"done", "error"}):
                break
            await asyncio.sleep(1.0)
        state = pairing_state(key)
        if state is None:
            return _err("pairing did not start — try again")
        if state.status == "done":
            return _ok(
                {
                    "status": "connected",
                    "identity": state.identity,
                    "next": (
                        "WhatsApp is linked. Ensure a 'platform:whatsapp' channel exists "
                        "on you (self_connect_platform) and restart (self_restart)."
                    ),
                }
            )
        if state.status == "error":
            return _err(state.detail or "pairing failed")
        if state.qr_data:
            path = _save_qr_png(agent, state.qr_data)
            return _ok(
                {
                    "status": "scan_the_qr",
                    "qr_image": path,
                    "pair_code_hint": (
                        "Prefer a number instead? The dashboard's WhatsApp connector "
                        "also offers a phone-number pairing code."
                    ),
                    "instructions": connector.instructions,
                    "note": "QR codes expire after ~1 minute — call this tool again for a fresh one.",
                }
            )
        return _ok(
            {
                "status": state.status or "starting",
                "detail": state.detail
                or "The WhatsApp bridge is starting — ask again in a moment.",
            }
        )
    except Exception as exc:
        return _err(exc)


@tool_metadata(capability="write_external", risk="high", requires_hitl=True)
@tool
async def self_connect_weixin() -> str:
    """Connect yourself to WeChat (Weixin) by QR code — the default path, no token.

    Call this IMMEDIATELY when the user asks to connect WeChat/Weixin, the
    personal messenger (what a plain "WeChat" means). NOT for WeCom (Enterprise
    WeChat / WeChat Work), a company bot wired with
    ``self_connect_platform('platform:wecom', …)`` only when the user names it.
    This tool
    starts the pairing and returns the QR as an image saved in your workspace
    — mention the returned ``qr_image`` path in your reply and it renders
    inline in the chat (and ships as a picture on messaging channels). The
    user opens WeChat, taps Scan (扫一扫), scans the code and confirms on the
    phone. Tencent issues the credentials on scan and they are stored
    server-side automatically — there is no token to ask the user for
    (``WEIXIN_APP_TOKEN`` is only an advanced manual fallback for
    multi-account servers; never lead with it).

    Pairing keeps running in the background: call this tool again to refresh
    the QR (they expire) or to check whether the scan landed. Once it reports
    connected, the ``platform:weixin`` channel is saved onto you
    automatically, pinned to the scanned account — finish with
    ``self_restart`` when the user is ready.
    """
    try:
        agent, denial = _gate("connect_platforms", mutating=True)
        if denial:
            return denial
        from olano.platforms.channel_setup import get_connector, pairing_state

        connector = get_connector("wechat")
        if connector is None:
            return _err("WeChat support is not available on this deployment")
        key = f"selfcfg:{agent}:wechat"
        state = pairing_state(key)
        if state is None or state.status in {"done", "error"}:
            await connector.start_pairing(key, agent, {})
        deadline = time.monotonic() + _QR_WAIT_SECONDS
        while time.monotonic() < deadline:
            state = pairing_state(key)
            if state is not None and (state.qr_data or state.status in {"done", "error"}):
                break
            await asyncio.sleep(1.0)
        state = pairing_state(key)
        if state is None:
            return _err("pairing did not start — try again")
        if state.status == "done":
            saved = _pin_weixin_channel(agent, state.identity)
            return _ok(
                {
                    "status": "connected",
                    "identity": state.identity,
                    "channel_saved": saved,
                    "next": (
                        "WeChat is linked and the 'platform:weixin' channel is saved on "
                        "you, pinned to the scanned account. Restart (self_restart) when "
                        "the user is ready and the connection goes live."
                    ),
                }
            )
        if state.status == "error":
            return _err(state.detail or "pairing failed")
        if state.qr_data:
            path = _save_qr_png(agent, state.qr_data, prefix="weixin-qr")
            return _ok(
                {
                    "status": "scan_the_qr",
                    "qr_image": path,
                    "instructions": connector.instructions,
                    "note": (
                        "QR codes expire after a short while — call this tool again for "
                        "a fresh one, or to check whether the scan landed."
                    ),
                }
            )
        return _ok(
            {
                "status": state.status or "starting",
                "detail": state.detail
                or "Contacting the WeChat login service — ask again in a moment.",
            }
        )
    except Exception as exc:
        return _err(exc)


def _pin_weixin_channel(agent: str, account_id: str) -> dict[str, Any]:
    """Save the agent's ``platform:weixin`` channel pinned to *account_id*.

    The QR credential store is server-global, so the entry must say which
    linked login is THIS agent's (``extra.bot_id`` — same pin the dashboard's
    pairing modal writes). Keeps an existing entry's other settings and every
    other channel; goes through the validated admin writer like every other
    self-mutation.
    """
    from olano.config.loader import load_agent_config

    cfg = load_agent_config(agent)
    channels: list[Any] = []
    entry: dict[str, Any] = {"type": "platform:weixin"}
    for ch in cfg.channels or []:
        if isinstance(ch, str):
            if ch.strip() != "platform:weixin":
                channels.append(ch)
            continue
        dumped = ch.model_dump() if hasattr(ch, "model_dump") else dict(ch)
        if str(dumped.get("type")) == "platform:weixin":
            entry = dumped
            continue
        channels.append(dumped)
    if account_id:
        entry["extra"] = {**(entry.get("extra") or {}), "bot_id": account_id}
    channels.append(entry)
    return admin.update_agent(agent, {"channels": channels})


def _save_qr_png(agent: str, data: str, prefix: str = "whatsapp-qr") -> str:
    """Render the pairing QR into the agent's workspace and return its path.

    An absolute path inside the workspace is what the chat's media detection
    resolves, so the QR shows as an image in the dashboard conversation and
    ships as a picture on messaging channels. **The agent has to put the path
    in its reply** — detection reads the finished message, never the tool
    result (`runtime/media_refs.SCAN_TOOL_RESULTS`), which is why both callers'
    docstrings tell it to mention ``qr_image``.
    """
    import segno

    from olano.config.home import get_olano_paths

    ws = get_olano_paths().agent_workspace_dir(agent)
    out_dir = ws / "uploads"
    dir_existed = out_dir.exists()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{prefix}-{int(time.time())}.png"
    segno.make(data, error="m").save(str(path), scale=6, border=2)
    handoff_to_sandbox(path, created=() if dir_existed else (out_dir,), agent=agent)
    return str(path)


# ── toolkit registry contract ────────────────────────────────────────────────

_ALL_TOOLS = [
    self_get_config,
    self_set_external_access,
    self_list_archetypes,
    self_read_archetype,
    self_list_models,
    self_set_model,
    self_update_prompt,
    self_restart,
    self_list_skills,
    self_enable_skill,
    self_disable_skill,
    self_list_subagents,
    self_add_subagent,
    self_search_mcp_directory,
    self_list_mcp_servers,
    self_list_mcp_tools,
    self_reload_mcp_servers,
    self_add_mcp_server,
    self_remove_mcp_server,
    self_authorize_mcp_server,
    self_find_integration,
    self_add_toolkit,
    self_remove_toolkit,
    self_inspect_external,
    self_list_platforms,
    self_connect_platform,
    self_connect_whatsapp,
    self_connect_weixin,
]


def get_tools(names: list[str] | None = None) -> list[Any]:
    if names is None:
        return list(_ALL_TOOLS)
    wanted = set(names)
    return [t for t in _ALL_TOOLS if t.name in wanted]


def get_tool(name: str) -> Any | None:
    return next((t for t in _ALL_TOOLS if t.name == name), None)
