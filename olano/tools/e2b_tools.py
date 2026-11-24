"""E2B sandboxed code execution toolkit.

Auth env var: E2B_API_KEY (sign up at e2b.dev).

Runs untrusted/agent-generated code inside an isolated Linux microVM —
much safer than running through ``safe_shell`` or ``node_run`` on the host.

Each tool call spins up a fresh sandbox by default. Pass ``session_id`` to
reuse a long-running sandbox across calls (state survives, including
installed packages and files).
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_E2B_CREDS_MSG = "[missing-credentials] E2B_API_KEY not set in vault."

# Cache of long-lived sandboxes by session_id, so subsequent calls reuse state.
_SESSIONS: dict[str, object] = {}


def _open_sandbox(session_id: str = "") -> tuple[Any, str | None]:
    """Open (or reuse) an E2B sandbox. Returns (sandbox, error_string)."""
    sdk, err = optional_import("e2b_code_interpreter", purpose="E2B sandboxed code execution")
    if err:
        # Fall back to legacy package name
        sdk, err2 = optional_import("e2b", purpose="E2B sandboxed code execution")
        if err2:
            return None, err
    if not get_credential("E2B_API_KEY"):
        return None, _E2B_CREDS_MSG
    try:
        Sandbox = getattr(sdk, "Sandbox", None) or getattr(sdk, "CodeInterpreter", None)
        if Sandbox is None:
            return None, "[tool-error] e2b: SDK does not expose Sandbox class."
        if session_id:
            if session_id in _SESSIONS:
                return _SESSIONS[session_id], None
            sbx = Sandbox()
            _SESSIONS[session_id] = sbx
            return sbx, None
        return Sandbox(), None
    except Exception as exc:
        return None, f"[tool-error] e2b init: {exc}"


def _close_if_ephemeral(sbx: object, session_id: str) -> None:
    if session_id:
        return  # keep alive
    close = getattr(sbx, "kill", None) or getattr(sbx, "close", None)
    if close:
        try:
            close()
        except Exception:
            pass


@tool
def e2b_run_code(
    code: Annotated[str, "Python source to execute"],
    timeout: Annotated[int, "Max seconds (1-300)"] = 60,
    session_id: Annotated[str, "Reuse an existing sandbox by id (empty = ephemeral)"] = "",
) -> str:
    """Execute Python code inside an E2B sandbox and return stdout/stderr/result."""
    sbx, err = _open_sandbox(session_id)
    if err:
        return err
    try:
        timeout = max(1, min(timeout, 300))
        # The newer code-interpreter SDK exposes `run_code`.
        run = getattr(sbx, "run_code", None) or getattr(sbx, "notebook", None)
        if run is None:
            return "[tool-error] e2b_run_code: sandbox missing run_code/notebook API."
        if hasattr(sbx, "run_code"):
            execution = sbx.run_code(code, timeout=timeout)
        else:
            execution = sbx.notebook.exec_cell(code)

        # Newer SDK exposes logs.stdout/.stderr and .results
        stdout_lines = getattr(getattr(execution, "logs", None), "stdout", []) or []
        stderr_lines = getattr(getattr(execution, "logs", None), "stderr", []) or []
        results = getattr(execution, "results", []) or []
        error = getattr(execution, "error", None)

        out_text = "".join(stdout_lines) if isinstance(stdout_lines, list) else str(stdout_lines)
        err_text = "".join(stderr_lines) if isinstance(stderr_lines, list) else str(stderr_lines)
        result_text = "\n".join(
            getattr(r, "text", None) or str(r) for r in results if r is not None
        )

        sections = []
        if out_text:
            sections.append(f"[stdout]\n{out_text.rstrip()}")
        if err_text:
            sections.append(f"[stderr]\n{err_text.rstrip()}")
        if result_text:
            sections.append(f"[result]\n{result_text.rstrip()}")
        if error:
            name = getattr(error, "name", None) or "Error"
            value = getattr(error, "value", None) or str(error)
            sections.append(f"[exception] {name}: {value}")
        if not sections:
            sections.append("(no output)")
        return "\n\n".join(sections)
    except Exception as exc:
        return f"[tool-error] e2b_run_code: {exc}"
    finally:
        _close_if_ephemeral(sbx, session_id)


@tool
def e2b_install_package(
    package: Annotated[str, "PyPI package spec (e.g. 'pandas' or 'numpy==2.0.0')"],
    session_id: Annotated[str, "Reuse an existing sandbox by id (empty = ephemeral)"] = "",
) -> str:
    """Install a pip package inside an E2B sandbox. Pass session_id to keep it installed."""
    if not session_id:
        return (
            "[tool-error] e2b_install_package: session_id is required — installing into "
            "an ephemeral sandbox is wasted work because the sandbox is destroyed at "
            "the end of the call. Pass a stable session_id and reuse it for run_code."
        )
    sbx, err = _open_sandbox(session_id)
    if err:
        return err
    try:
        run = getattr(sbx, "run_code", None)
        if run is None:
            return "[tool-error] e2b_install_package: sandbox missing run_code API."
        execution = run(f"%pip install --quiet {package}")
        error = getattr(execution, "error", None)
        if error:
            return f"[tool-error] e2b_install_package: {getattr(error, 'value', str(error))}"
        return f"Installed {package} in sandbox '{session_id}'."
    except Exception as exc:
        return f"[tool-error] e2b_install_package: {exc}"


@tool
def e2b_close_session(
    session_id: Annotated[str, "Session ID to terminate"],
) -> str:
    """Terminate a long-lived E2B sandbox to release resources."""
    sbx = _SESSIONS.pop(session_id, None)
    if sbx is None:
        return f"No active E2B session '{session_id}'."
    close = getattr(sbx, "kill", None) or getattr(sbx, "close", None)
    if close:
        try:
            close()
        except Exception as exc:
            return f"[tool-error] e2b_close_session: {exc}"
    return f"Closed E2B session '{session_id}'."


get_tools, get_tool = make_toolkit_api(globals())
