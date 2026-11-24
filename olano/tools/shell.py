"""Safe shell tool: execute shell commands with an allowlist and timeout.

Execution goes through ``tools/_sandbox_exec.sandboxed_run``: inside the
agent's live sandbox when it has one, on the host only under the open posture.
In an agent turn the security middleware's shell chokepoint
(``deepagents_middleware._run_shell_command``) intercepts the call before this
body runs; the body enforces the same rule itself so that no caller outside
the graph can turn it into a host shell on a managed box.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Annotated

from langchain_core.tools import tool

from olano.security.tool_metadata import tool_metadata
from olano.tools._sandbox_exec import host_execution_refused, sandboxed_run

# Commands that are NEVER allowed regardless of config
_BLOCKLIST = {
    "rm",
    "rmdir",
    "dd",
    "mkfs",
    "fdisk",
    "parted",
    "kill",
    "killall",
    "shutdown",
    "reboot",
    "poweroff",
    "halt",
    "format",
    "deltree",
    "curl",  # blocked by default; allow via config
    "wget",
    "nc",
    "netcat",
    "nmap",
}

# Commands that are allowed by default
_ALLOWLIST = {
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "awk",
    "sed",
    "echo",
    "pwd",
    "env",
    "printenv",
    "which",
    "date",
    "wc",
    "sort",
    "uniq",
    "tr",
    "cut",
    "paste",
    "diff",
    "git",
    "python",
    "python3",
    "pip",
    "uv",
    "node",
    "npm",
    "npx",
    "yarn",
    "jq",
    "yq",
    "make",
    "docker",
    "docker-compose",
    # A detached tmux session outlives this tool's 120s ceiling, so a long build
    # or server can be started here and its output read back later with
    # `tmux capture-pane -p -t <name>` instead of dying with the call.
    "tmux",
}

_READ_ONLY_ALLOWLIST = {
    "ls",
    "cat",
    "head",
    "tail",
    "grep",
    "find",
    "awk",
    "sed",
    "echo",
    "pwd",
    "env",
    "printenv",
    "which",
    "date",
    "wc",
    "sort",
    "uniq",
    "tr",
    "cut",
    "paste",
    "diff",
}

_SHELL_CONTROL_PATTERN = re.compile(r"(\|\||&&|[|;<>`])|\$\(|\$\{")


def extract_base_command(command: str) -> str:
    """Return the normalized base command name from a shell command string."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return ""
    if not parts:
        return ""
    return parts[0].split("/")[-1]


def classify_shell_command_risk(command: str) -> str:
    """
    Return a coarse risk tier for a shell command: low, medium, or high.

    Low-risk commands are intentionally narrow: simple read-only commands with no
    shell control operators. Everything else remains manual-review territory.
    """
    stripped = command.strip()
    if not stripped:
        return "high"
    if _SHELL_CONTROL_PATTERN.search(stripped):
        return "medium"

    base_cmd = extract_base_command(stripped)
    if not base_cmd:
        return "high"
    if base_cmd in _BLOCKLIST:
        return "high"
    if base_cmd in _READ_ONLY_ALLOWLIST:
        return "low"
    if base_cmd in _ALLOWLIST:
        return "medium"
    return "medium"


def is_low_risk_shell_command(command: str) -> bool:
    """Return True when the command is a simple read-only shell operation."""
    return classify_shell_command_risk(command) == "low"


@tool_metadata(capability="mutating", risk="high", requires_hitl=True)
@tool("safe_shell")
def safe_shell_tool(
    command: Annotated[str, "Shell command to execute"],
    timeout: Annotated[int, "Timeout in seconds (max 120)"] = 30,
) -> str:
    """
    Execute a shell command safely with allowlist enforcement and timeout.
    Returns stdout + stderr combined. Blocked commands return an error message.
    """
    # Deployment entitlements, checked per invocation so a remote settings
    # change bites agents that were built while shell was still enabled. The
    # builder's resolver is the one definition of the exec gate and never
    # raises: a resolver failure on a box that looks managed reads as "no
    # shell", where a bare `feature_enabled` used to fail open here while the
    # backend build failed closed on the same question.
    from olano.core.builder import _resolve_exec_posture

    shell_allowed, _managed_exec, _sandbox_ok = _resolve_exec_posture()
    if not shell_allowed:
        return (
            "Error: shell commands are disabled for this deployment "
            "(deployment settings: features.shell)."
        )

    timeout = min(timeout, 120)
    try:
        parts = shlex.split(command)
    except ValueError as exc:
        return f"[tool-error] safe_shell: invalid command syntax: {exc}"
    if not parts:
        return "Error: empty command"

    base_cmd = parts[0].split("/")[-1]  # strip path

    if base_cmd in _BLOCKLIST:
        return f"Error: command '{base_cmd}' is not allowed for safety reasons."

    if base_cmd not in _ALLOWLIST:
        return f"Error: command '{base_cmd}' is not in the allowlist. Allowed: {sorted(_ALLOWLIST)}"

    try:
        # Sandbox when live, host only on an open box (platform secrets
        # stripped, interpreter shims appended last so `python` resolves on a
        # python3-only host), a refusal on a managed box with no sandbox.
        result = sandboxed_run(parts, timeout=timeout)
        if host_execution_refused(result):
            return f"Error: {result.stderr}"
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Error: command timed out after {timeout}s"
    except FileNotFoundError:
        return f"Error: command '{base_cmd}' not found"
    except Exception as e:
        return f"Error executing command: {e}"


from olano.tools._toolkit import make_toolkit_api

get_tools, get_tool = make_toolkit_api(globals())
