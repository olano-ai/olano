"""The Cortex/Pulse list endpoints carry the paths the chat hand-off drafts.

The dashboard's "Chat" / "Explore more" buttons pre-fill the composer of a new
thread with the item's **full workspace path** so the agent opens the artifacts
instead of hunting for them. The index stores a Cortex item's ``path`` relative
to the workspace and a Pulse card's ``references`` relative to its agent's
workspace, and nothing in the browser knows where that workspace is — so the
API says: ``abs_path`` per Cortex item, ``workspace_root`` per Pulse card.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.cognition import cortex_items as ci
from olano.config.agent_config import AgentConfig
from olano.config.workspace import Workspace
from olano.pulse.engine import PulseEngine
from olano.runtime.api import create_api


@pytest.fixture
def workspace(tmp_path):
    ws = Workspace(root=tmp_path / "ws")
    ws.init()
    return ws


@pytest.fixture
def client(workspace):
    config = AgentConfig(name="scout", workspace=str(workspace.root))
    runner = MagicMock()
    runner.config = config
    runner.ainvoke = AsyncMock(return_value="ok")
    runtime = MagicMock()
    runtime._runners = {"scout": runner}
    runtime.get_runner.return_value = runner
    runtime.scheduler = None
    runtime._self_learning = []
    return TestClient(create_api(runtime))


def test_cortex_items_carry_their_absolute_folder(client, workspace) -> None:
    item = ci.add_item(workspace.root, kind="idea", title="Solar kiln", summary="dry wood")
    assert not item["path"].startswith("/"), "the index stores a relative path"

    listed = client.get("/api/cortex/items?agent=scout").json()["items"]
    (row,) = [it for it in listed if it["id"] == item["id"]]
    assert row["path"] == item["path"]
    assert row["abs_path"] == str(workspace.root / item["path"])

    detail = client.get(f"/api/cortex/items/{item['id']}?agent=scout").json()["item"]
    assert detail["abs_path"] == str(workspace.root / item["path"])


def test_cortex_item_without_a_folder_never_resolves_to_the_workspace_root(
    client, workspace
) -> None:
    # An older/degenerate index row with an empty path must not become "<root>/"
    # — that would point the agent at the whole tree as the item's artifacts.
    item = ci.add_item(workspace.root, kind="idea", title="Pathless", write_detail=False)
    data = ci._load(workspace.root)
    for it in data["items"]:
        if it["id"] == item["id"]:
            it["path"] = ""
    ci._save(workspace.root, data)

    listed = client.get("/api/cortex/items?agent=scout").json()["items"]
    (row,) = [it for it in listed if it["id"] == item["id"]]
    assert row["abs_path"] == ""


def test_cortex_items_all_agents_stamps_each_item_with_its_owner(tmp_path) -> None:
    """The fleet-wide list names the agent on every row — the index never does.

    Two agents, two workspaces: the dashboard's all-agents Cortex review must
    be able to tell whose idea each card is, route its actions to that agent,
    and count per agent for the filter — none of which the per-workspace index
    records. The single-agent form keeps its shape and gains the same stamp.
    """
    ws_a = Workspace(root=tmp_path / "a")
    ws_a.init()
    ws_b = Workspace(root=tmp_path / "b")
    ws_b.init()
    runners = {}
    for name, ws in (("scout", ws_a), ("archer", ws_b)):
        runner = MagicMock()
        runner.config = AgentConfig(name=name, workspace=str(ws.root))
        runners[name] = runner
    runtime = MagicMock()
    runtime._runners = runners
    runtime.get_runner.side_effect = lambda n: runners[n]
    runtime.scheduler = None
    runtime._self_learning = []
    client = TestClient(create_api(runtime))

    idea = ci.add_item(ws_a.root, kind="idea", title="Solar kiln", summary="dry wood")
    proto = ci.add_item(ws_b.root, kind="prototype", title="Kiln sensor", summary="probe")

    body = client.get("/api/cortex/items?all_agents=true&include_discarded=true").json()
    assert body["agent"] == ""
    assert set(body["agents"]) == {"scout", "archer"}
    owners = {it["id"]: it["agent"] for it in body["items"]}
    assert owners[idea["id"]] == "scout"
    assert owners[proto["id"]] == "archer"
    # abs_path resolves against the OWNER's workspace, not the default agent's.
    (row,) = [it for it in body["items"] if it["id"] == proto["id"]]
    assert row["abs_path"] == str(ws_b.root / proto["path"])
    assert body["counts"]["idea"] == 1
    assert body["counts"]["prototype"] == 1
    assert body["counts_by_agent"]["scout"]["idea"] == 1
    assert body["counts_by_agent"]["archer"]["prototype"] == 1
    assert body["counts_by_agent"]["archer"]["idea"] == 0

    # `kind` still narrows the fleet list.
    ideas = client.get("/api/cortex/items?all_agents=true&kind=idea").json()["items"]
    assert [it["id"] for it in ideas] == [idea["id"]]

    # The one-agent form is unchanged apart from the stamp.
    single = client.get("/api/cortex/items?agent=archer").json()
    assert single["agent"] == "archer"
    assert [it["agent"] for it in single["items"]] == ["archer"]
    assert "counts_by_agent" not in single


def test_pulse_cards_name_their_agents_workspace_root(client, workspace) -> None:
    engine = PulseEngine(
        AgentConfig(name="scout", workspace=str(workspace.root)), workspace.state_dir
    )
    engine.add(
        "A v1 readiness one-pager.",
        title="Connector readiness matrix",
        references=["memories/readiness.md"],
    )

    items = client.get("/api/pulse?agent=scout").json()["items"]
    (card,) = [it for it in items if it["title"] == "Connector readiness matrix"]
    assert card["agent"] == "scout"
    assert card["references"] == ["memories/readiness.md"]
    assert card["workspace_root"] == str(workspace.root)
