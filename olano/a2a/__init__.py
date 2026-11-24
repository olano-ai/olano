"""Outbound Agent2Agent (A2A) protocol client.

Lets an Olano agent hand work to an **external** agent — another vendor's, or
one on a different Olano deployment — over Google's open A2A protocol, using
the official ``a2a-sdk``. This package is the *outbound* half of
interoperability; the inbound half (other platforms reaching Olano agents) is
:mod:`olano.mcp_server`. Neither is how Olano agents talk to each other: that
stays in-process via ``talk_to_agent``.

Reach is an operator decision, never the model's: only agents listed in the
calling agent's ``agent.json5 → remote_agents`` are addressable, every URL is
policed for HTTPS and SSRF before a byte leaves the box, credentials come from
the vault, and every exchange lands in the same activity log the Agent to
Agent dashboard view reads.
"""

from olano.a2a.client import (
    RemoteAgentError,
    RemoteAgentPolicyError,
    RemoteAgentReply,
    discover_agent_card,
    get_remote_task,
    list_remote_agents,
    send_remote_message,
)

__all__ = [
    "RemoteAgentError",
    "RemoteAgentPolicyError",
    "RemoteAgentReply",
    "discover_agent_card",
    "get_remote_task",
    "list_remote_agents",
    "send_remote_message",
]
