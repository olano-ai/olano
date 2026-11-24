"""The browser HTTP surface (Config → Browser, the agent's Browser tab, the live
view socket) and the tool gate on every surface that lists tools."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from olano.browser import policy


@pytest.fixture
def box(monkeypatch):
    state = {"features": {"browser": True, "browser_live": True}, "ncpu": 4, "mem": 8 * policy.GIB}
    monkeypatch.setattr(policy, "_feature", lambda name: state["features"].get(name, True))
    monkeypatch.setattr(policy, "_host", lambda: (state["ncpu"], state["mem"]))
    monkeypatch.setattr(policy, "_docker_available", lambda: True)
    monkeypatch.setattr(policy, "available_memory_bytes", lambda: 4 * policy.GIB)
    from olano.browser import sidecar

    pulls: list[bool] = []
    monkeypatch.setattr(sidecar, "start_pull", lambda force=False: pulls.append(True) or {})
    monkeypatch.setattr(
        sidecar,
        "image_status",
        lambda: {
            "image": "olano-browser:latest",
            "source": "x",
            "present": False,
            "docker": True,
            "pull": {},
        },
    )
    state["pulls"] = pulls
    return state


@pytest.fixture
def client(box):
    from olano.runtime.api import create_api

    return TestClient(create_api(MagicMock()))


def test_system_config_reports_the_block_and_its_effect(client):
    body = client.get("/api/system/config").json()
    assert body["browser"]["mode"] == "off"
    assert body["browser_status"]["availability"]["code"] == "disabled"
    assert body["browser_status"]["live_entitled"] in (True, False)


def test_put_validates_every_field(client):
    for bad in (
        {"mode": "turbo"},
        {"provider": "moon"},
        {"max_sessions": 0},
        {"max_sessions": True},
        {"idle_timeout": 241},
        {"timezone": "Mars/Olympus"},
        {"timezone": 5},
    ):
        r = client.put("/api/system/config", json={"browser": bad})
        assert r.status_code == 400, bad


def test_put_saves_and_clears_the_timezone(client):
    from olano.config.system_settings import load_browser_settings

    assert (
        client.put(
            "/api/system/config", json={"browser": {"timezone": " Europe/Madrid "}}
        ).status_code
        == 200
    )
    assert load_browser_settings().timezone == "Europe/Madrid"
    assert client.get("/api/system/config").json()["browser"]["timezone"] == "Europe/Madrid"
    assert client.put("/api/system/config", json={"browser": {"timezone": ""}}).status_code == 200
    assert load_browser_settings().timezone == ""


def test_put_refuses_on_a_one_gigabyte_box(client, box):
    box["mem"] = int(0.95 * policy.GIB)
    r = client.put("/api/system/config", json={"browser": {"mode": "headless"}})
    assert r.status_code == 400
    assert "memory" in r.json()["detail"]
    # The remote provider costs this machine nothing, so it is allowed.
    ok = client.put(
        "/api/system/config", json={"browser": {"mode": "headless", "provider": "remote"}}
    )
    assert ok.status_code == 200


def test_put_refuses_live_without_the_plan(client, box):
    box["features"]["browser_live"] = False
    r = client.put("/api/system/config", json={"browser": {"mode": "live"}})
    assert r.status_code == 400 and "plan" in r.json()["detail"]


def test_turning_live_on_starts_the_image_pull(client, box):
    r = client.put("/api/system/config", json={"browser": {"mode": "live", "max_sessions": 2}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["browser"]["mode"] == "live" and body["browser"]["max_sessions"] == 2
    assert box["pulls"] == [True]


def test_agent_browser_endpoint(client):
    body = client.get("/api/agents/merli/browser").json()
    assert body["session"] is None
    assert body["availability"]["enabled"] is False
    assert body["live_view"] is False


def test_take_control_needs_live(client, box):
    from olano.config.system_settings import set_browser_settings

    set_browser_settings({"mode": "headless"})
    r = client.post("/api/agents/merli/browser/control", json={"who": "user"})
    assert r.status_code == 409
    assert client.post("/api/agents/merli/browser/control", json={"who": "x"}).status_code == 400


def test_live_socket_refuses_when_live_is_off(client):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/agents/merli/browser/live") as ws:
            ws.receive_text()
    assert exc.value.code == 4403


def test_live_socket_refuses_without_a_session(client):
    from olano.config.system_settings import set_browser_settings

    set_browser_settings({"mode": "live"})
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/api/agents/merli/browser/live") as ws:
            ws.receive_text()
    assert exc.value.code == 4404


def test_admin_config_path_runs_the_same_enable_check(box):
    from olano.admin.config import _apply_browser
    from olano.config.system_settings import load_browser_settings

    box["mem"] = int(0.95 * policy.GIB)
    wrote, ignored = _apply_browser({"mode": "headless"})
    assert not wrote and ignored and ignored[0]["key"] == "browser.mode"
    assert load_browser_settings().mode == "off"


# ── the tool gate on every surface ───────────────────────────────────────────


def test_resolve_tools_skips_browser_tools_while_off(box):
    from olano.config.system_settings import set_browser_settings
    from olano.tools.registry import resolve_tools

    names = ["browser_navigate", "page_screenshot", "web_fetch"]
    assert [t.name for t in resolve_tools(names)] == ["web_fetch"]
    set_browser_settings({"mode": "headless"})
    assert {t.name for t in resolve_tools(names)} == set(names)


def test_effective_names_whoami_and_prompt_agree(box, base_config):
    from olano.config.ops_defaults import resolve_effective_tool_names
    from olano.config.system_settings import set_browser_settings
    from olano.core.prompt_assembler import PromptAssembler

    cfg = base_config.model_copy(update={"tools": [*base_config.tools, "browser_navigate"]})
    assert "browser_navigate" not in resolve_effective_tool_names(cfg.tools, cfg.disabled_tools)
    assembler = PromptAssembler.__new__(PromptAssembler)
    assembler.config = cfg
    assert "browser_navigate" not in assembler._visible_tool_names()
    set_browser_settings({"mode": "headless"})
    assert "browser_navigate" in resolve_effective_tool_names(cfg.tools, cfg.disabled_tools)
    assert "browser_navigate" in assembler._visible_tool_names()


def test_catalog_marks_browser_tools_unavailable(client):
    rows = client.get("/api/catalog/tools").json()["tools"]
    nav = next((r for r in rows if r["name"] == "browser_navigate"), None)
    assert nav is not None and nav["unavailable"]
    other = next(r for r in rows if r["name"] not in policy.BROWSER_TOOL_NAMES)
    assert other["unavailable"] == ""
