"""End to end: an oversized MCP result reaches the model as a content-aware preview.

The unit tests in ``test_tool_result_preview.py`` call the preview directly and
``test_context_window_policy.py`` calls the cap middleware directly. This test
runs the whole chain with no API key: ``build_agent_with_mcp`` connects a real
stdio MCP server (``tests/fixtures/mcp_large_result_server.py``), the offline
``fake:`` model scripts two tool calls, the security pipeline marks the results
untrusted, ``McpResultCapMiddleware`` offloads them, and the test reads the
messages the model was actually sent on its final call.

Both payloads hide one failed record in the middle, which the old head and tail
preview could never show: a failed profile in a one-line JSON document and a
declined order on line 513 of a CSV export.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from langchain_core.messages import ToolMessage

from olano.config.agent_config import AgentConfig, MemoryConfig
from olano.config.backend_config import FilesystemBackendConfig

pytest.importorskip("mcp.server.fastmcp")

SERVER = Path(__file__).parent / "fixtures" / "mcp_large_result_server.py"


def _payloads():
    spec = importlib.util.spec_from_file_location("mcp_large_result_server", SERVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.people_payload(), module.orders_payload()


def _offload_path(content: str) -> Path:
    for line in content.splitlines():
        if "saved to: " in line:
            return Path(line.split("saved to: ", 1)[1].strip())
    raise AssertionError("no offload path in the tool message")


async def test_oversized_mcp_results_reach_the_model_as_content_aware_previews(
    tmp_workspace, monkeypatch
):
    from olano.core.builder import build_agent_with_mcp
    from olano.core.runner import AgentRunner
    from olano.models import fake as fake_mod

    monkeypatch.setenv(
        "LIONCLAW_FAKE_MODEL_SCRIPT",
        json.dumps(
            [
                {"tool": "search_people", "args": {"query": "vp sales"}},
                {"tool": "export_orders", "args": {}},
                {"text": "done"},
            ]
        ),
    )
    seen: list[list] = []
    original = fake_mod.FakeChatModel._next_step

    def spy(self, messages):
        seen.append(list(messages))
        return original(self, messages)

    monkeypatch.setattr(fake_mod.FakeChatModel, "_next_step", spy)

    config = AgentConfig(
        name="e2e-offload",
        description="MCP offload preview end to end",
        model="fake:instant",
        system_prompt="You are a test assistant.",
        workspace=str(tmp_workspace),
        memory=MemoryConfig(markdown=True, rag=False, self_learning=False),
        backend=FilesystemBackendConfig(root_dir=str(tmp_workspace), virtual_mode=False),
        mcp_servers=[
            {
                "name": "large-results",
                "url": "stdio://olano-large-results",
                "transport": "stdio",
                "command": sys.executable,
                "args": [str(SERVER)],
            }
        ],
    )
    agent = await build_agent_with_mcp(config)
    runner = AgentRunner(agent, config)
    reply = ""
    async for etype, payload in runner.astream_events("find people", thread_id="offload-e2e"):
        if etype == "text":
            reply += payload
    assert reply.strip() == "done"
    assert len(seen) == 3

    sent = {m.name: m for m in seen[-1] if isinstance(m, ToolMessage)}
    assert set(sent) == {"search_people", "export_orders"}
    people, orders = _payloads()

    for name, original_text in (("search_people", people), ("export_orders", orders)):
        content = sent[name].content
        assert isinstance(content, str)
        # The security marker stays first; the offload note follows.
        assert content.startswith("[trusted=false]\nTool result too large")
        # The file holds the whole result (with the marker, when the security
        # pipeline ran before the cap).
        path = _offload_path(content)
        on_disk = path.read_text(encoding="utf-8").removeprefix("[trusted=false]\n")
        assert on_disk == original_text
        # The model is sent a fraction of the result.
        assert len(content) < len(original_text) / 5

    people_note = sent["search_people"].content
    assert "Preview (JSON object with 2 keys; arrays shortened: $.people" in people_note
    assert '"id":"p-0037"' in people_note  # the profile in an error state
    assert '"total_entries":4812' in people_note  # the pagination block

    orders_note = sent["export_orders"].content
    assert "Preview (CSV table, 900 rows × 5 columns" in orders_note
    failed_line = orders.splitlines()[512]
    assert "FAILED card declined" in failed_line
    assert f"   513\t{failed_line}" in orders_note  # at its own line number
    assert "rows left out: all currency=SGD" in orders_note
