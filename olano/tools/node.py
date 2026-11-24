"""Node.js bridge tool: run Node scripts and CLIs from agent context.

Both tools execute through ``tools/_sandbox_exec.sandboxed_run``: inside the
agent's live sandbox when it has one, on the host only under the open posture,
a refusal on a managed box with no sandbox. In an agent turn the security
middleware's shell chokepoint intercepts these calls before the bodies run;
the bodies enforce the same rule so no caller outside the graph gets a host
process out of them.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path
from typing import Annotated

from langchain_core.tools import tool

from olano.security.tool_metadata import tool_metadata
from olano.tools._sandbox_exec import (
    host_execution_refused,
    sandbox_active_for_current_agent,
    sandboxed_run,
)


def _find_node() -> str | None:
    return shutil.which("node")


def _find_npx() -> str | None:
    return shutil.which("npx")


def _resolve_cli(name: str) -> str | None:
    """The executable to invoke for ``name`` on the lane the command will run on.

    In the sandbox the bare name is resolved by the container's own PATH (the
    shared tool layer first); a host `which` would hand it a path from the
    runtime image that may not exist there. On the host lane the `which` is
    what turns a missing install into a clear error instead of ENOENT.
    """
    if sandbox_active_for_current_agent():
        return name
    return shutil.which(name)


@tool_metadata(capability="mutating", risk="high", requires_hitl=True)
@tool
def node_run_tool(
    script_or_command: Annotated[
        str,
        "Either a path to a .js file or an npx CLI command (e.g. 'prettier --write file.js')",
    ],
    args: Annotated[list[str] | None, "Additional arguments"] = None,
    cwd: Annotated[str | None, "Working directory"] = None,
    timeout: Annotated[int, "Timeout in seconds"] = 60,
) -> str:
    """
    Run a Node.js script or npx CLI tool.

    Examples:
        node_run_tool("scripts/parse.js", args=["input.json"])
        node_run_tool("prettier --write src/index.js")
    """
    if args is None:
        args = []
    node = _resolve_cli("node")
    npx = _resolve_cli("npx")

    if node is None:
        return "Error: Node.js is not installed or not in PATH."

    cmd_parts = shlex.split(script_or_command)
    if not cmd_parts:
        return "Error: empty command"
    first = cmd_parts[0]

    # Detect: is this a file path ending in .js? The workspace is mounted at
    # its host path inside the sandbox, so the host-side check holds there too.
    if first.endswith(".js") and Path(first).exists():
        full_cmd = [node, first] + list(args)
    else:
        # Treat as npx command
        if npx is None:
            return "Error: npx is not available."
        full_cmd = [npx, "--yes"] + cmd_parts + list(args)

    try:
        result = sandboxed_run(full_cmd, timeout=timeout, cwd=cwd)
        if host_execution_refused(result):
            return f"Error: {result.stderr}"
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: node command timed out after {timeout}s"
    except Exception as e:
        return f"Error running node command: {e}"


@tool_metadata(capability="mutating", risk="high", requires_hitl=True)
@tool
def npm_install_tool(
    package: Annotated[str, "npm package to install (e.g. 'prettier' or 'prettier@3.2.0')"],
    global_install: Annotated[bool, "Install globally with -g"] = False,
    cwd: Annotated[str | None, "Working directory for local installs"] = None,
) -> str:
    """Install an npm package."""
    npm = _resolve_cli("npm")
    if npm is None:
        return "Error: npm is not installed."

    cmd = [npm, "install"]
    if global_install:
        cmd.append("-g")
    cmd.append(package)

    try:
        result = sandboxed_run(cmd, timeout=120, cwd=cwd)
        if host_execution_refused(result):
            return f"Error: {result.stderr}"
        return result.stdout + (f"\n[stderr]\n{result.stderr}" if result.stderr else "")
    except Exception as e:
        return f"Error: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
