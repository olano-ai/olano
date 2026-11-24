"""A keyless local stdio MCP server, for measuring what an MCP server costs.

``scripts/bench_agent_memory.py --mcp-local N`` points every benchmarked agent
at this file so the MCP cost can be measured with no credential, no network and
no directory entry — which matters because a *remote* MCP server's cost is
mostly invisible to the box (the tools arrive as JSON), while a *stdio* one
spawns a process per agent whose RSS is charged to the same machine.

``--tools N`` sizes the advertised toolset so per-tool cost separates from
per-server cost.
"""

from __future__ import annotations

import argparse


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--tools", type=int, default=8)
    args = p.parse_args()

    from mcp.server.fastmcp import FastMCP

    server = FastMCP("olano-bench")

    def make(i: int):
        def fn(query: str = "", limit: int = 10) -> str:
            """Benchmark no-op. Returns its own index and the query it was given."""
            return f"bench tool {i}: query={query!r} limit={limit}"

        fn.__name__ = f"bench_tool_{i:02d}"
        fn.__doc__ = (
            f"Benchmark tool {i}. Does no I/O and needs no credential; exists so an "
            f"agent build has a realistic MCP toolset to resolve, validate and bind."
        )
        return fn

    for i in range(args.tools):
        server.add_tool(make(i))

    server.run(transport="stdio")


if __name__ == "__main__":
    main()
