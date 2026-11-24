"""The team digest: what the other agents on this deployment can do.

A background run that needs to send an email, post to Slack, query Linear or
read a CRM should ask the teammate that holds that capability instead of
noting "needs email" in a STATUS file. For that it has to know who holds what,
and the peer roster the chat prompt carries only names and descriptions. This
module renders the same roster with each peer's **capabilities**: the service
toolkits it is bound to (gmail, calendar, hubspot, github, ...), whether it has
a shell, and the MCP servers it is connected to with their tool counts.

Visibility is the operator's, not ours: :func:`olano.tools.talk_to.peer_roster`
applies the agent's ``agent_visibility`` mode and the org chart exactly as the
chat prompt does, so a background run never learns about a peer the same agent
could not see in a conversation, and never is told it can message one it may
not. ``capability_summary`` is shared with the prompt assembler's roster line
so chat and background runs describe a teammate in the same words.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from olano.config.agent_config import AgentConfig

#: Peers one digest lists; a fleet larger than this is summarised by its most
#: capable members (messageable first).
MAX_PEERS = 12
#: Capability labels per peer line before "and N more".
MAX_LABELS = 8


def capability_summary(peer: dict[str, Any]) -> str:
    """``can: gmail, calendar; shell; MCP: linear (14 tools), notion (needs authorization)``.

    Empty for a peer with nothing beyond the universal tools, so a roster line
    for a plain conversational agent stays as it was.
    """
    parts: list[str] = []
    labels = [str(t) for t in (peer.get("toolkits") or []) if t]
    if labels:
        shown = labels[:MAX_LABELS]
        more = len(labels) - len(shown)
        parts.append("can: " + ", ".join(shown) + (f" and {more} more" if more > 0 else ""))
    if peer.get("has_shell") and not peer.get("is_admin"):
        parts.append("shell")
    servers = [s for s in (peer.get("mcp") or []) if isinstance(s, dict) and s.get("name")]
    if servers:
        rendered: list[str] = []
        for s in servers[:MAX_LABELS]:
            status = str(s.get("status") or "")
            count = int(s.get("tools") or 0)
            if status == "connected" or (s.get("ok") and count):
                rendered.append(f"{s['name']} ({count} tool{'s' if count != 1 else ''})")
            elif status == "auth_required":
                rendered.append(f"{s['name']} (needs authorization)")
            else:
                rendered.append(f"{s['name']} ({status or 'down'})")
        parts.append("MCP: " + ", ".join(rendered))
    return "; ".join(parts)


def _peer_line(peer: dict[str, Any], *, messageable: bool) -> str:
    name = str(peer.get("name") or "")
    label = str(peer.get("label") or "").strip()
    title = str(peer.get("title") or "").strip()
    who = f"`{name}`"
    if label and label != name:
        who = f"{label} ({who})"
    if title and title != label:
        who += f", {title}"
    desc = str(peer.get("description") or "").strip().split("\n", 1)[0]
    caps = capability_summary(peer) or "no service tools beyond the defaults"
    reach = "" if messageable else " [visible only: you cannot message this agent from here]"
    line = f"- {who}{reach}: {desc}" if desc else f"- {who}{reach}"
    return f"{line}. {caps[0].upper() + caps[1:]}."


def team_digest(config: AgentConfig, *, max_peers: int = MAX_PEERS) -> str:
    """The team as one block of context for a background worker, or ``""``
    when this agent is allowed to see no other agent."""
    try:
        from olano.tools.talk_to import peer_roster

        roster = peer_roster(config)
    except Exception:
        return ""
    talk = list(roster.get("talk") or [])
    aware = list(roster.get("aware") or [])
    if not talk and not aware:
        return ""
    lines = ["Team: the other agents on this deployment and what each can do."]
    if talk:
        lines.append(
            'Ask one with talk_to_agent("<handle>", "<exact request, file paths included>") '
            "when a step needs a capability you lack (an email, a post, a system only it "
            "can reach). The reply IS the result: record it in the item folder. One clear "
            "ask per teammate; never hand over work you can do yourself, and never ask a "
            "teammate to do something the owner has not authorised for you."
        )
    for p in talk[:max_peers]:
        lines.append(_peer_line(p, messageable=True))
    for p in aware[: max(0, max_peers - len(talk))]:
        lines.append(_peer_line(p, messageable=False))
    hidden = len(talk) + len(aware) - min(len(talk) + len(aware), max_peers)
    if hidden > 0:
        lines.append(f"(and {hidden} more; the chat roster lists them all)")
    return "\n".join(lines)
