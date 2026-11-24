"""``pages_deploy`` is gated by the security middleware exactly like a shell call.

Its build/start strings run later, in the agent's sandbox, for as long as the
page is up, so an agent with no shell must not get one through a page, the
shell guard must see the commands, and a managed box without a sandbox must
refuse. All of that is checked before the tool body records anything.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import ToolMessage
from langgraph.prebuilt.tool_node import ToolCallRequest

import olano.deployment.enforcement as enf
from olano.config.agent_config import AgentConfig
from olano.pages import policy
from olano.security import deepagents_middleware as mw


def _config(tmp_path: Path, *, shell: bool) -> AgentConfig:
    from olano.config.backend_config import FilesystemBackendConfig, LocalShellBackendConfig

    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    (workspace / "MEMORY.md").write_text("# Memory\n")
    (workspace / "memories").mkdir(exist_ok=True)
    kwargs: dict[str, Any] = {
        "name": "gate-agent",
        "model": "anthropic:claude-haiku-4-5-20251001",
        "system_prompt": "gate",
        "workspace": str(workspace),
        "memory": {"markdown": False, "rag": False, "self_learning": False},
    }
    kwargs["backend"] = (
        LocalShellBackendConfig(root_dir=str(workspace))
        if shell
        else FilesystemBackendConfig(root_dir=str(workspace))
    )
    return AgentConfig(**kwargs)


def _request(args: dict[str, Any]) -> ToolCallRequest:
    return ToolCallRequest(
        tool_call={"name": "pages_deploy", "args": args, "id": "call-1"},
        tool=None,
        state={},
        runtime=SimpleNamespace(),
    )


@pytest.fixture
def open_posture(monkeypatch):
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: False)
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)


async def _call(config: AgentConfig, args: dict[str, Any]) -> tuple[Any, AsyncMock]:
    handler = AsyncMock(return_value=ToolMessage(content="recorded", tool_call_id="call-1"))
    middleware = mw.OlanoSecurityMiddleware(config=config, workspace_root=Path(config.workspace))
    result = await middleware.awrap_tool_call(_request(args), handler)
    return result, handler


async def test_agent_without_shell_cannot_deploy(tmp_path, open_posture):
    result, handler = await _call(
        _config(tmp_path, shell=False), {"slug": "crm", "directory": "app", "start": "node s.js"}
    )
    assert isinstance(result, ToolMessage) and policy.NO_SHELL in str(result.content)
    handler.assert_not_called()


async def test_shell_guard_sees_page_commands(tmp_path, open_posture):
    result, handler = await _call(
        _config(tmp_path, shell=True),
        {"slug": "crm", "directory": "app", "build": "rm -rf /", "output": "dist"},
    )
    assert "Blocked by shell guard" in str(result.content)
    handler.assert_not_called()


async def test_managed_box_without_sandbox_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(enf, "require_sandboxed_exec", lambda: True)
    monkeypatch.setattr(policy, "live_sandbox", lambda agent: None)
    monkeypatch.setattr(policy, "_registered_sandbox", lambda agent: None)
    result, handler = await _call(
        _config(tmp_path, shell=True), {"slug": "crm", "directory": "app", "start": "node s.js"}
    )
    assert policy.NO_SANDBOX_MANAGED in str(result.content)
    handler.assert_not_called()


async def test_allowed_deploy_reaches_the_tool(tmp_path, open_posture):
    result, handler = await _call(
        _config(tmp_path, shell=True), {"slug": "crm", "directory": "app", "start": "node s.js"}
    )
    handler.assert_awaited_once()
    assert result.content == "recorded"


async def test_switch_off_refuses_before_the_tool(tmp_path, open_posture):
    from olano.config.system_settings import set_pages_settings

    set_pages_settings({"services_enabled": False})
    result, handler = await _call(
        _config(tmp_path, shell=True), {"slug": "crm", "directory": "app", "start": "node s.js"}
    )
    assert policy.SERVICES_DISABLED in str(result.content)
    handler.assert_not_called()
