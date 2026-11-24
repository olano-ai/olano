"""Inbound MCP server — Olano agents as tools for the outside world.

Publishes every agent flagged ``external_access.mcp: true`` as a tool on one Model
Context Protocol endpoint (``<public URL>/api/mcp``, Streamable HTTP), so an
external platform that speaks MCP — Claude, ChatGPT, Cursor, another agent
framework — can send an Olano agent a message and read its reply.

Built on the official ``mcp`` Python SDK, which already ships the OAuth 2.1
*resource server* half of the MCP authorization spec: bearer-token middleware,
RFC 9728 protected-resource metadata, the 401/403 ``WWW-Authenticate``
contract, and DNS-rebinding protection for the transport. Olano supplies the
token verifier (operator-minted bearer tokens, hashed at rest, plus optional
JWTs from an external authorization server), the tool surface, and the audit
trail: every inbound call runs as a normal agent turn on an ``mcp::`` thread,
under the agent's own approval rules, and lands in the Agent to Agent view.

This is the *inbound* half of interoperability; the outbound half is
:mod:`olano.a2a`. Neither is how Olano agents talk to each other.
"""

from olano.mcp_server.server import OlanoMcpServer, mcp_endpoint_path
from olano.mcp_server.tokens import McpTokenStore, get_token_store

__all__ = ["McpTokenStore", "OlanoMcpServer", "get_token_store", "mcp_endpoint_path"]
