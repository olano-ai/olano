"""Asks an Olano agent a question over MCP, with the official `mcp` SDK.

    export OLANO_MCP_URL=<public URL>/api/mcp   OLANO_MCP_TOKEN=<a token from External access>
    python mcp-ask.py ask_<agent> "the question"
"""
import asyncio
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client


async def main(tool: str, question: str) -> None:
    url, token = os.environ["OLANO_MCP_URL"], os.environ["OLANO_MCP_TOKEN"]
    async with streamablehttp_client(url, headers={"Authorization": f"Bearer {token}"}) as (read, write, _):
        async with ClientSession(read, write) as session:
            server = await session.initialize()
            print(f"connected: {server.serverInfo.name}, {url}", flush=True)
            tools = await session.list_tools()
            print("tools: " + ", ".join(t.name for t in tools.tools), flush=True)
            print(f"calling {tool} ...", flush=True)
            result = await session.call_tool(tool, {"message": question})
            for block in result.content:
                print(("error: " if result.isError else "") + block.text, flush=True)


asyncio.run(main(sys.argv[1], sys.argv[2]))
