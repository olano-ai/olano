"""Offload oversized MCP tool results to a workspace file.

An MCP server answers with whatever it likes: one Apollo people search is
25 profiles of nested JSON, one Composio "search tools" call ships a plan the
size of a small essay. Nothing on the olano side capped those, so a five-message
conversation reached 150k tokens of context and the compaction that followed
threw away the start of the session. deepagents' own offload
(``FilesystemMiddleware``, ``tool_token_limit_before_evict``) is the right
mechanism but the wrong number for MCP: it fires at 20k tokens per result,
which is four Apollo pages, and it writes through the deepagents backend at a
path that is not writable on a filesystem-backed agent (see
``builder._ensure_filesystem_offload_patch``).

This middleware is the MCP-specific cap. It wraps every tool call whose name
belongs to a connected MCP server (the builder passes the set), estimates the
result at four characters per token, and above ``token_limit`` writes the full
text to ``<workspace state dir>/large_tool_results/<tool_call_id>`` (the
workspace's generated-state folder, ``Workspace.state_dir``, where shell-run
logs and the workspace map already live) with plain host I/O (the workspace is
mounted at its host path inside a sandbox, and ``handoff_to_sandbox`` re-owns
the file to the sandbox uid), then replaces the result with a path note and a
content-aware preview (``tool_result_preview.build_preview``: JSON keeps its
structure with long arrays sampled, tables and logs keep the header, the ends,
every error line and one example of each kind, with counted summaries of what
was left out). The reading procedure is deepagents' (``read_file`` with offset
and limit, or ``grep``), so it is the same for both offloads.

Two invariants. **The ``[trusted=false]`` marker survives**: the security
pipeline prefixes every MCP result with it so the model treats the payload as
data; the replacement note keeps the marker as its first line and the offloaded
file carries the original text unchanged. **A write failure keeps the original
message**: an unwritable workspace must degrade to the old behaviour, never to
a preview pointing at a file that does not exist.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import ToolMessage

from olano.core.tool_result_preview import build_preview

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4
OFFLOAD_DIRNAME = "large_tool_results"
UNTRUSTED_MARKER = "[trusted=false]"

TOO_LARGE_MCP_RESULT = """Tool result too large ({tokens} tokens, this agent's MCP result limit is {limit}). The full result of tool call {tool_call_id} was saved to: {file_path}

Read it with the read_file tool a part at a time (offset and limit parameters, e.g. offset=0 and limit=100 for the first 100 lines), or grep that file for the fields you need. Ask for fewer results per call next time when the tool allows it (a per_page or limit argument). Answer from the preview below when it already holds what you need.

{content_sample}"""


def _extract_text(message: ToolMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "\n".join(parts)
    return str(content) if content is not None else ""


def _sanitize_id(tool_call_id: str) -> str:
    keep = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool_call_id)
    return keep[:120] or "unknown"


def estimate_tokens(text: str) -> int:
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


class McpResultCapMiddleware(AgentMiddleware):
    """Replace an oversized MCP tool result with a preview + offloaded file."""

    def __init__(
        self,
        *,
        tool_names: set[str],
        token_limit: int,
        offload_dir: Path,
        agent_name: str | None = None,
    ) -> None:
        super().__init__()
        self._names = set(tool_names)
        self._limit = int(token_limit)
        self._dir = Path(offload_dir)
        self._agent = agent_name

    # ── the cap ──────────────────────────────────────────────────────────────

    def _over_limit(self, request: Any, result: Any) -> bool:
        """The cheap test: an MCP tool's message over the limit."""
        if not isinstance(result, ToolMessage):
            return False  # a Command, or something a later middleware built
        try:
            name = str(request.tool_call.get("name") or "")
        except Exception:
            return False
        return name in self._names and estimate_tokens(_extract_text(result)) > self._limit

    def _cap(self, request: Any, result: Any) -> Any:
        if not self._over_limit(request, result):
            return result
        name = str(request.tool_call.get("name") or "")
        text = _extract_text(result)
        tokens = estimate_tokens(text)
        tool_call_id = str(result.tool_call_id or "") or str(request.tool_call.get("id") or "")
        path = self._dir / _sanitize_id(tool_call_id)
        if not self._write(path, text):
            return result
        replacement = TOO_LARGE_MCP_RESULT.format(
            tokens=tokens,
            limit=self._limit,
            tool_call_id=tool_call_id,
            file_path=str(path),
            content_sample=build_preview(text),
        )
        if text.startswith(UNTRUSTED_MARKER):
            replacement = f"{UNTRUSTED_MARKER}\n{replacement}"
        logger.info(
            "MCP result from %s offloaded (%d tokens > %d) to %s", name, tokens, self._limit, path
        )
        return result.model_copy(update={"content": replacement})

    def _write(self, path: Path, text: str) -> bool:
        try:
            from olano.tools._sandbox_exec import handoff_to_sandbox, missing_parents

            created = missing_parents(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            handoff_to_sandbox(path, created=created, agent=self._agent)
            return True
        except Exception as exc:
            logger.warning("MCP result offload to %s failed; keeping it inline: %s", path, exc)
            return False

    # ── langchain hooks ──────────────────────────────────────────────────────

    def wrap_tool_call(self, request: Any, handler: Callable[[Any], Any]) -> Any:
        return self._cap(request, handler(request))

    async def awrap_tool_call(self, request: Any, handler: Callable[[Any], Awaitable[Any]]) -> Any:
        result = await handler(request)
        if not self._over_limit(request, result):
            return result
        # Writing the file, handing it to the sandbox and building the preview
        # are blocking work on a large payload: keep them off the event loop.
        return await asyncio.to_thread(self._cap, request, result)


def build_mcp_result_cap_middleware(
    *,
    mcp_summary: list[dict[str, Any]] | None,
    token_limit: int,
    state_dir: Path | str,
    agent_name: str | None = None,
) -> list[AgentMiddleware]:
    """The cap middleware for an agent's live MCP tools, or nothing.

    ``state_dir`` is the workspace's generated-state folder
    (``Workspace.state_dir``); the offload directory is created beneath it.
    Nothing when the limit is ``0`` (the operator's opt-out) or no MCP server
    connected: the common no-MCP agent pays no per-tool-call overhead.
    """
    if not token_limit or token_limit <= 0 or not mcp_summary:
        return []
    names: set[str] = set()
    for row in mcp_summary:
        if row.get("ok"):
            names.update(str(n) for n in (row.get("tools") or []) if n)
    if not names:
        return []
    offload_dir = Path(state_dir) / OFFLOAD_DIRNAME
    return [
        McpResultCapMiddleware(
            tool_names=names,
            token_limit=token_limit,
            offload_dir=offload_dir,
            agent_name=agent_name,
        )
    ]
