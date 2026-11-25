"""A minimal ACP client: it starts an agent the way an editor does and sends it prompts."""

import asyncio
import os
import sys
from pathlib import Path

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.schema import (
    AllowedOutcome,
    ClientCapabilities,
    Implementation,
    RequestPermissionResponse,
    TextContentBlock,
)


class Editor:
    """The editor's side: the agent's updates arrive here as they stream."""

    async def session_update(self, session_id, update, **_):
        kind = update.session_update
        if kind == "agent_message_chunk":
            print(update.content.text, end="", flush=True)
        elif kind == "tool_call":
            print(f"[tool] {update.title}", flush=True)
        elif kind == "tool_call_update" and update.status:
            print(f"[tool] {update.status}", flush=True)

    async def request_permission(self, options, session_id, tool_call, **_):
        print(f"[permission] {tool_call.title}", flush=True)
        for n, option in enumerate(options, 1):
            print(f"  {n}. {option.name}", flush=True)
        print("  choice: ", end="", flush=True)
        pick = options[int((await asyncio.to_thread(sys.stdin.readline)).strip()) - 1]
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=pick.option_id, outcome="selected")
        )


async def main(agent):
    command = ["olano", "acp", "--agent", agent]
    async with spawn_agent_process(Editor(), *command, env=os.environ) as (conn, _):
        init = await conn.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="acp-client", version="0.1"),
        )
        print(f"started: {' '.join(command)}")
        print(f"connected: ACP protocol version {init.protocol_version}")
        session = await conn.new_session(cwd=str(Path.cwd()), mcp_servers=[])
        print(f"session: {session.session_id[:8]}")
        while True:
            print("\n> ", end="", flush=True)
            prompt = (await asyncio.to_thread(sys.stdin.readline)).strip()
            if not prompt:
                break
            done = await conn.prompt(
                session_id=session.session_id, prompt=[TextContentBlock(type="text", text=prompt)]
            )
            print(f"\n[{done.stop_reason}]", flush=True)


asyncio.run(main(sys.argv[1]))
