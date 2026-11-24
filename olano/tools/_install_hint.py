"""Shared helper for tool functions that depend on optional Python packages.

Tool implementations used to `raise ImportError("Install: uv add foo")` on a
missing dependency. That bubbled up as a hard error string the calling agent
couldn't reason about beyond echoing it back to the user. This helper turns
the same situation into a structured tool-result string so the agent can:

  • tell the user clearly what's missing and why,
  • offer to install it via shell access (if it has it), or
  • delegate the install to a peer agent that does.

Usage:

    @tool
    def yahoo_get_quote(symbol: str) -> str:
        yf, err = optional_import("yfinance", purpose="real-time Yahoo Finance quotes")
        if err:
            return err
        ...
"""

from __future__ import annotations

import importlib
from typing import Any


def _detect_admin_agent() -> str | None:
    """Look for a booted peer agent that smells like an IT Admin.

    Priority order so the right agent gets recommended even when several
    have shell access:
      1. an agent literally named ``it-admin`` or ``admin``,
      2. any agent whose name contains "admin" with safe_shell in tools,
      3. any agent with safe_shell in tools (last resort — only if there's
         exactly one — so we don't accidentally point at a developer agent).
    """
    try:
        from olano.tools.talk_to import _RUNTIME

        if _RUNTIME is None:
            return None
        runners = getattr(_RUNTIME, "_runners", {})
        # Explicit name match wins.
        for preferred in ("it-admin", "admin", "IT Admin"):
            if preferred in runners:
                return preferred
        # Agents with "admin" in their name + shell tool.
        admin_like: list[str] = []
        all_shells: list[str] = []
        for name, runner in runners.items():
            cfg = getattr(runner, "config", None)
            tools = getattr(cfg, "tools", []) or []
            if "safe_shell" in tools:
                all_shells.append(name)
                if "admin" in name.lower():
                    admin_like.append(name)
        if admin_like:
            return admin_like[0]
        # Only auto-pick a shell-capable peer when there's exactly one — more
        # than one is ambiguous and could surprise the user.
        if len(all_shells) == 1:
            return all_shells[0]
    except Exception:
        pass
    return None


def _caller_has_talk_to_agent() -> bool:
    """Return True if the agent that triggered this install_hint can call
    `talk_to_agent` (so we know whether 'ask it-admin' is a real option for
    them)."""
    try:
        from olano.tools.talk_to import _RUNTIME, current_agent

        if _RUNTIME is None:
            return False
        caller = current_agent.get() or ""
        if not caller:
            return False
        runner = getattr(_RUNTIME, "_runners", {}).get(caller)
        if runner is None:
            return False
        tools = list(getattr(runner.config, "tools", []) or [])
        return "talk_to_agent" in tools
    except Exception:
        return False


def install_hint(package: str, *, purpose: str = "", pip_name: str | None = None) -> str:
    """Return a structured tool-result that drives a *deterministic* user-
    facing response. The string is templated so the model only has to swap
    in the package name and reproduce the markdown verbatim — no
    paraphrasing, no inventing tools, no silent delegation.

    The user always sees two clear options:
      1) install locally via the dashboard's ▶ Run button, then click
         "Send to agent" to pipe the output back, OR
      2) delegate to the IT Admin via `talk_to_agent` (with HITL
         approval) — only shown when both the caller has talk_to_agent
         AND an admin-like peer is currently booted.
    """
    why = f" needed for {purpose}" if purpose else ""
    # The pip/uv install name often differs from the import name
    # (e.g. import `hubspot` ← package `hubspot-api-client`).
    pkg = pip_name or package
    admin = _detect_admin_agent()
    can_delegate = _caller_has_talk_to_agent() and bool(admin)

    if can_delegate:
        # Two-option template: self-install OR delegate.
        user_facing = (
            f"I can't install Python packages directly — my tool list doesn't "
            f"include shell access. Here are two ways to handle this:\n"
            f"\n"
            f"**Option 1 — install it yourself in one click**\n"
            f"```bash\n"
            f"uv add {pkg}\n"
            f"```\n"
            f"Click **▶ Run** on the code block above. After it finishes, hit "
            f"**Send to agent** so I can pick up where I left off.\n"
            f"\n"
            f"**Option 2 — let `{admin}` handle it (HITL-approved)**\n"
            f'Just reply **"ask {admin}"** and I\'ll delegate the install. '
            f"You'll get an approval card in the chat before anything runs."
        )
        directive = (
            f"Reply to the user with the markdown ABOVE — verbatim. Do not "
            f"call any tool yet. If they reply with anything that means "
            f'"yes, ask the admin" (e.g. "ask {admin}", "delegate it", '
            f'"go ahead"), THEN call:\n'
            f'  `talk_to_agent(agent_name="{admin}", '
            f'message="please run: uv add {pkg}")`\n'
            f"If they say they'll run it themselves (or send you the run output), "
            f"continue with the original task once the install lands."
        )
    else:
        # One-option template: only self-install. We show the path to enable
        # delegation but don't push it as a current option.
        bootstrap_hint = (
            "_(If you'd like, you can also create an IT Admin agent from the "
            "dashboard's New Agent flow → choose the `it-admin` template — "
            "future installs can then be delegated with one click.)_"
            if not admin
            else f"_(I'd normally offer to delegate to `{admin}`, but I don't have "
            f"the `talk_to_agent` tool. Adding it to my config lets me hand "
            f"installs off automatically.)_"
        )
        user_facing = (
            f"I can't install Python packages directly — my tool list doesn't "
            f"include shell access. Here's the easiest fix:\n"
            f"\n"
            f"**Install it yourself in one click**\n"
            f"```bash\n"
            f"uv add {pkg}\n"
            f"```\n"
            f"Click **▶ Run** on the code block above. After it finishes, hit "
            f"**Send to agent** so I can continue.\n"
            f"\n"
            f"{bootstrap_hint}"
        )
        directive = (
            "Reply to the user with the markdown ABOVE — verbatim. Do not "
            "call any tool. Once they install the package and send the output "
            "back, continue with the original task."
        )

    return (
        f"[missing-dependency] The Python package `{package}` is not installed{why}.\n"
        f"\n"
        f"=== USER-FACING REPLY (reproduce this in your next message) ===\n"
        f"{user_facing}\n"
        f"=== END REPLY ===\n"
        f"\n"
        f"Instructions for you (the agent):\n"
        f"{directive}\n"
        f"\n"
        f"Hard rules:\n"
        f"- Do NOT call `safe_shell`, `safe_shell_tool`, `exec`, `bash`, or any "
        f"other shell-style tool — they are NOT in your tool list.\n"
        f"- Do NOT paraphrase the reply above. Reproduce the markdown so the "
        f"dashboard can render its ▶ Run button on the code block."
    )


def optional_import(
    package: str, *, purpose: str = "", pip_name: str | None = None
) -> tuple[Any | None, str | None]:
    """Try to import ``package``. Returns (module, None) on success or
    (None, install_hint_string) on failure — never raises.

    ``pip_name`` overrides the install command when the import name differs
    from the distribution name (e.g. import ``hubspot`` ← ``hubspot-api-client``).
    """
    try:
        return importlib.import_module(package), None
    except ImportError:
        return None, install_hint(package, purpose=purpose, pip_name=pip_name)
