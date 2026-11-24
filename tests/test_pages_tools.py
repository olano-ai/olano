"""Tests for the pages agent toolkit (pages_start/status/stop/restart/delete)."""

from __future__ import annotations

import pytest

from olano.pages.registry import get_page, set_page_auth, start_page
from olano.security.request_context import push_request_context
from olano.tools import pages_tools
from olano.tools._credentials import agent_runtime_context


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "agent-ws"
    site = ws / "site"
    site.mkdir(parents=True)
    (site / "index.html").write_text("<h1>hi</h1>")
    return ws


@pytest.fixture
def ctx(workspace):
    """Bind agent 'alice' + her workspace for the duration of a tool call."""
    with agent_runtime_context("alice", {}):
        with push_request_context(source="test", metadata={"workspace_root": str(workspace)}):
            yield workspace


def test_page_start_publishes_and_reports_locked_state(ctx):
    out = pages_tools.pages_start.func(slug="My-Report", directory="site")
    assert "/pages/my-report/" in out
    assert "NOT SERVING YET" in out  # no viewer password on this host yet
    assert get_page("my-report")["agent"] == "alice"


def test_page_start_reports_configured_auth(ctx):
    # The bound agent is 'alice'; its own credential is what unlocks its pages.
    set_page_auth("alice", "owner", "long-enough-pw")
    out = pages_tools.pages_start.func(slug="report", directory="site")
    assert "Basic Auth" in out
    assert "NOT SERVING YET" not in out


def test_page_start_collision_returns_error_string(ctx):
    start_page("bob", "report", "site", ctx)
    out = pages_tools.pages_start.func(slug="report", directory="site")
    assert out.startswith("Error:")
    assert "bob" in out


def test_page_start_missing_directory_errors(ctx):
    out = pages_tools.pages_start.func(slug="report", directory="missing")
    assert out.startswith("Error:")


def test_page_start_notes_missing_index(ctx):
    (ctx / "bare").mkdir()
    out = pages_tools.pages_start.func(slug="bare-page", directory="bare")
    assert "no index.html" in out


def test_status_stop_restart_delete_lifecycle(ctx):
    pages_tools.pages_start.func(slug="report", directory="site")

    status = pages_tools.pages_status.func(slug="report")
    assert "serving" in status

    stopped = pages_tools.pages_stop.func(slug="report")
    assert "stopped" in stopped
    assert get_page("report")["enabled"] is False

    restarted = pages_tools.pages_restart.func(slug="report")
    assert "serving again" in restarted
    assert get_page("report")["enabled"] is True

    deleted = pages_tools.pages_delete.func(slug="report")
    assert "released" in deleted
    assert get_page("report") is None


def test_status_reports_availability_and_foreign_ownership(ctx):
    assert "available" in pages_tools.pages_status.func(slug="fresh-slug")
    start_page("bob", "taken", "site", ctx)
    assert "taken by agent 'bob'" in pages_tools.pages_status.func(slug="taken")
    # Foreign pages cannot be stopped/deleted through the tools.
    assert pages_tools.pages_stop.func(slug="taken").startswith("Error:")
    assert pages_tools.pages_delete.func(slug="taken").startswith("Error:")


def test_status_lists_own_pages_only(ctx):
    pages_tools.pages_start.func(slug="alice-page", directory="site")
    start_page("bob", "bobs-page", "site", ctx)
    out = pages_tools.pages_status.func(slug="")
    assert "alice-page" in out
    assert "bobs-page" not in out


def test_toolkit_registration():
    from olano.tools.registry import resolve_bundle, resolve_tools

    names = {t.name for t in resolve_bundle("pages")}
    assert names == {
        "pages_start",
        "pages_deploy",
        "pages_status",
        "pages_logs",
        "pages_stop",
        "pages_restart",
        "pages_delete",
    }
    # Individual names resolve too (that's what AgentConfig.tools entries use).
    assert [t.name for t in resolve_tools(["pages_start"])] == ["pages_start"]


# ── pages_deploy / pages_logs ────────────────────────────────────────────────


@pytest.fixture
def may_run(monkeypatch):
    """Open posture, shell granted: what an agent with a sandbox sees."""
    from olano.pages import policy

    monkeypatch.setattr(policy, "command_denial", lambda agent, config=None: None)


def test_deploy_records_a_service_without_running_anything_inline(ctx, may_run):
    (ctx / "app").mkdir()
    out = pages_tools.pages_deploy.func(slug="crm", directory="app", start="node server.js")
    assert "deployed from 'app'" in out and "pages_status('crm')" in out
    record = get_page("crm")
    assert record["kind"] == "service"
    assert record["deploy"]["start"] == "node server.js"
    assert record["generation"] == 1


def test_deploy_static_build_records_output_and_spa(ctx, may_run):
    (ctx / "web").mkdir()
    out = pages_tools.pages_deploy.func(
        slug="web", directory="web", build="npm ci && npm run build", output="dist", spa=True
    )
    assert "building in the background" in out
    record = get_page("web")
    assert record["kind"] == "static" and record["spa"] is True
    assert record["deploy"]["output"] == "dist"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({}, "build command, a start command"),
        ({"build": "npm run build"}, "needs output"),
        ({"start": "node s.js", "spa": True}, "spa applies to static"),
        ({"start": "rm -rf /"}, "start command refused"),
    ],
)
def test_deploy_refusals(ctx, may_run, kwargs, message):
    (ctx / "app").mkdir()
    out = pages_tools.pages_deploy.func(slug="crm", directory="app", **kwargs)
    assert out.startswith("Error:") and message in out
    assert get_page("crm") is None


def test_deploy_refused_without_command_access(ctx, monkeypatch):
    from olano.pages import policy

    monkeypatch.setattr(policy, "command_denial", lambda agent, config=None: policy.NO_SHELL)
    (ctx / "app").mkdir()
    out = pages_tools.pages_deploy.func(slug="crm", directory="app", start="node s.js")
    assert out == f"Error: {policy.NO_SHELL}"
    assert get_page("crm") is None


def test_deploy_respects_the_per_agent_server_limit(ctx, may_run):
    from olano.config.system_settings import set_pages_settings

    set_pages_settings({"max_services_per_agent": 1})
    (ctx / "app").mkdir()
    assert not pages_tools.pages_deploy.func(slug="one", directory="app", start="a").startswith(
        "Error"
    )
    out = pages_tools.pages_deploy.func(slug="two", directory="app", start="b")
    assert out.startswith("Error:") and "one" in out
    # Redeploying the same slug is not a second server.
    assert not pages_tools.pages_deploy.func(slug="one", directory="app", start="c").startswith(
        "Error"
    )


def test_logs_reads_the_page_logs(ctx, may_run):
    (ctx / "app").mkdir()
    pages_tools.pages_deploy.func(slug="crm", directory="app", start="node s.js")
    assert "empty" in pages_tools.pages_logs.func(slug="crm")
    log = ctx / ".olano" / "pages" / "crm" / "service.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(f"line {i}" for i in range(200)) + "\n")
    out = pages_tools.pages_logs.func(slug="crm", lines=3)
    assert out.endswith("line 197\nline 198\nline 199")
    assert ".olano/pages/crm/service.log" in out
    assert pages_tools.pages_logs.func(slug="crm", which="nope").startswith("Error:")
    start_page("bob", "bobs", "site", ctx)
    assert pages_tools.pages_logs.func(slug="bobs").startswith("Error:")


def test_logs_on_a_plain_page_explains(ctx):
    pages_tools.pages_start.func(slug="report", directory="site")
    assert "serves files only" in pages_tools.pages_logs.func(slug="report")


def test_status_reports_supervisor_state(ctx, may_run, monkeypatch):
    from olano.pages import services

    (ctx / "app").mkdir()
    pages_tools.pages_deploy.func(slug="crm", directory="app", start="node s.js")
    monkeypatch.setattr(
        services,
        "page_status",
        lambda slug: {
            "state": "failed",
            "detail": "stopped restarting after 5 crashes",
            "restarts": 5,
            "last_error": "Error: Cannot find module 'express'",
        },
    )
    out = pages_tools.pages_status.func(slug="crm")
    assert "service page: failed" in out and "5 restart(s)" in out
    assert "Cannot find module 'express'" in out


def test_start_warns_about_root_absolute_links(ctx):
    (ctx / "site" / "index.html").write_text('<script src="/assets/app.js"></script>')
    out = pages_tools.pages_start.func(slug="report", directory="site")
    assert "root-absolute" in out


def test_deploy_is_gated_like_a_shell_tool():
    from olano.security.hitl_policy import tool_category
    from olano.security.tool_metadata import infer_tool_metadata

    assert tool_category("pages_deploy") == "shell"
    assert infer_tool_metadata("pages_deploy").capability == "mutating"
