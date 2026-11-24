"""Outbound-destination lists: ``egress_allow`` / ``egress_deny`` at two levels.

The resolution rule is the whole product decision, so it is pinned here as
three facts: deny is the union, allow is the agent's list when it has one
(else the deployment's), and empty everywhere is open. The rest pins the two
contracts the lists share with the ``sandbox:`` block (a hand-edited
config.yaml never raises on the tool path; a dashboard save never persists
something other than what was typed) and that the tool gate really reads the
resolved lists rather than the agent's ``security.ssrf_*`` fields alone.
"""

from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient
from langchain_core.tools import tool

from olano.config.agent_config import AgentConfig
from olano.config.home import load_home_config
from olano.security.egress import (
    ALLOW_KEY,
    DENY_KEY,
    EgressRuleError,
    agent_egress_lists,
    deployment_egress_lists,
    normalize_domain,
    normalize_domains,
    resolve_egress_policy,
    security_metadata,
)
from olano.security.request_context import get_request_context


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_path))
    return tmp_path


# ── normalisation ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("example.com", "example.com"),
        ("Example.COM", "example.com"),
        ("  api.github.com  ", "api.github.com"),
        ("https://Example.com/some/path?q=1", "example.com"),
        ("http://example.com:8080", "example.com"),
        ("example.com:443", "example.com"),
        ("example.com/path", "example.com"),
        ("*.example.com", "example.com"),
        (".example.com", "example.com"),
        ("example.com.", "example.com"),
        ("10.0.0.5", "10.0.0.5"),
        ("", None),
        ("   ", None),
        (None, None),
        ("*.", None),
    ],
)
def test_normalize_domain_forgives_how_people_write_hosts(raw, expected):
    assert normalize_domain(raw) == expected


@pytest.mark.parametrize("raw", ["not a host", "exa mple.com", "foo..bar", "-bad.com", "a b/c"])
def test_normalize_domain_rejects_what_is_not_a_host(raw):
    with pytest.raises(EgressRuleError):
        normalize_domain(raw)


def test_normalize_domains_dedupes_in_first_seen_order():
    assert normalize_domains(["B.com", "a.com", "b.com", "", "https://a.com/x"], strict=True) == [
        "b.com",
        "a.com",
    ]


def test_normalize_domains_strict_raises_and_lenient_drops(caplog):
    with pytest.raises(EgressRuleError):
        normalize_domains(["ok.com", "not a host"], strict=True)
    with caplog.at_level(logging.WARNING):
        assert normalize_domains(["ok.com", "not a host"], strict=False) == ["ok.com"]
    assert "not a host" in caplog.text


# ── the three-line rule ──────────────────────────────────────────────────────


def test_empty_everywhere_is_open():
    policy = resolve_egress_policy(AgentConfig(name="a"), deployment=([], []))
    assert policy.is_open
    assert policy.allow_source == "open"
    assert policy.as_request_metadata() == {ALLOW_KEY: [], DENY_KEY: []}


def test_agent_with_no_opinion_inherits_the_deployment_allow_list():
    policy = resolve_egress_policy(AgentConfig(name="a"), deployment=(["dep.com"], []))
    assert policy.allow == ("dep.com",)
    assert policy.allow_source == "deployment"


def test_agent_allow_list_replaces_the_deployment_one():
    cfg = AgentConfig(name="a", egress_allow=["mine.io"])
    policy = resolve_egress_policy(cfg, deployment=(["dep.com"], []))
    assert policy.allow == ("mine.io",)
    assert policy.allow_source == "agent"
    assert "dep.com" not in policy.allow


def test_deny_is_the_union_so_an_agent_can_never_undo_a_deployment_block():
    cfg = AgentConfig(name="a", egress_deny=["Agent-Bad.com"])
    policy = resolve_egress_policy(cfg, deployment=([], ["dep-bad.com"]))
    assert policy.deny == ("dep-bad.com", "agent-bad.com")
    # An agent that sets only an allow list still carries the deployment deny.
    cfg2 = AgentConfig(name="b", egress_allow=["dep-bad.com"])
    policy2 = resolve_egress_policy(cfg2, deployment=([], ["dep-bad.com"]))
    assert policy2.deny == ("dep-bad.com",)


def test_legacy_spellings_are_unioned_into_the_agent_lists():
    """``security.ssrf_*`` (the original per-agent names) and the sandbox
    block's ``egress_*`` (accepted for a long time, wired to nothing) both
    count, so a list written under any name keeps working."""
    cfg = AgentConfig(
        name="a",
        egress_allow=["new.com"],
        security={"ssrf_allowed_domains": ["legacy.io"], "ssrf_blocked_domains": ["old-bad.net"]},
        backend={
            "type": "lionclaw_docker",
            "egress_allow": ["sandbox.dev"],
            "egress_deny": ["sandbox-bad.dev"],
        },
    )
    allow, deny = agent_egress_lists(cfg)
    assert allow == ["new.com", "legacy.io", "sandbox.dev"]
    assert deny == ["old-bad.net", "sandbox-bad.dev"]


def test_none_config_is_open():
    assert resolve_egress_policy(None, deployment=([], [])).is_open


# ── the agent config is strict ───────────────────────────────────────────────


def test_agent_config_normalizes_and_rejects_loudly():
    cfg = AgentConfig(name="a", egress_allow=["https://API.GitHub.com/repos", "*.example.com"])
    assert cfg.egress_allow == ["api.github.com", "example.com"]
    with pytest.raises(ValueError):
        AgentConfig(name="b", egress_deny=["not a host"])


def test_agent_fields_are_top_level_in_the_schema_with_help_text():
    """The Advanced tab renders a top-level list[str] as a list control with
    the field's description as its hint — a nested block would collapse to a
    raw JSON editor. The description is the user-facing contract."""
    props = AgentConfig.model_json_schema()["properties"]
    for key in ("egress_allow", "egress_deny"):
        assert props[key]["type"] == "array"
        assert "deployment" in props[key]["description"]
        assert "sandbox" in props[key]["description"]  # says what it does NOT govern


# ── the deployment file is lenient ───────────────────────────────────────────


def test_missing_config_file_is_open(home):
    assert deployment_egress_lists() == ([], [])


def test_deployment_lists_are_read_leniently(home, caplog):
    (home / "config.yaml").write_text(
        "egress_allow:\n  - Good.com\n  - 'not a host'\negress_deny:\n  - https://bad.net/x\n"
    )
    with caplog.at_level(logging.WARNING):
        assert deployment_egress_lists() == (["good.com"], ["bad.net"])
    assert "not a host" in caplog.text
    # And the same file is what HomeConfig surfaces to the dashboard, verbatim.
    hc = load_home_config()
    assert hc.egress_allow == ["Good.com", "not a host"]


def test_malformed_deployment_block_is_open(home):
    (home / "config.yaml").write_text("egress_allow: 42\negress_deny: {a: 1}\n")
    assert deployment_egress_lists() == ([], [])


def test_resolve_reads_the_deployment_file_by_default(home):
    (home / "config.yaml").write_text("egress_allow: [dep.com]\n")
    policy = resolve_egress_policy(AgentConfig(name="a"))
    assert policy.allow == ("dep.com",)
    assert policy.allow_source == "deployment"


# ── the tool gate reads the resolved lists ───────────────────────────────────


def _context_probe():
    @tool
    def probe() -> str:
        """Return the security metadata the gate pushed for this call."""
        meta = get_request_context().metadata.get("security", {})
        return f"{sorted(meta.get(ALLOW_KEY, []))}|{sorted(meta.get(DENY_KEY, []))}"

    return probe


def test_pipeline_wrapper_pushes_the_resolved_lists(home):
    """The wrapped tool sees deployment ∪ agent, not the agent's ``security``
    block alone — and it is resolved per call, so a config.yaml edit reaches
    the next call with no rebuild."""
    from olano.security.pipeline import wrap_tool_with_security

    (home / "config.yaml").write_text("egress_allow: [dep.com]\negress_deny: [dep-bad.com]\n")
    cfg = AgentConfig(name="a", egress_deny=["mine-bad.com"])
    wrapped = wrap_tool_with_security(_context_probe(), cfg)

    assert wrapped.invoke({}) == "['dep.com']|['dep-bad.com', 'mine-bad.com']"

    (home / "config.yaml").write_text("egress_allow: [other.com]\n")
    assert wrapped.invoke({}) == "['other.com']|['mine-bad.com']"


async def test_pipeline_wrapper_pushes_the_resolved_lists_async(home):
    from olano.security.pipeline import wrap_tool_with_security

    (home / "config.yaml").write_text("egress_deny: [dep-bad.com]\n")
    cfg = AgentConfig(name="a", egress_allow=["mine.io"])
    wrapped = wrap_tool_with_security(_context_probe(), cfg)
    assert await wrapped.ainvoke({}) == "['mine.io']|['dep-bad.com']"


def test_security_metadata_keys_are_what_the_gated_tools_read():
    """``web_fetch``/``http_request``/… read these two keys; the policy module
    owns the spelling so nothing can drift."""
    meta = security_metadata(AgentConfig(name="a", egress_allow=["x.com"]))
    assert set(meta) == {"ssrf_allowlist", "ssrf_blocklist"}


def test_web_fetch_refuses_a_host_outside_the_allow_list(home):
    """End to end through the real gate: the SSRF validator refuses a host not
    on the resolved allow list before any request is made."""
    from olano.security.pipeline import wrap_tool_with_security
    from olano.tools.web_fetch import web_fetch

    (home / "config.yaml").write_text("egress_allow: [allowed.example]\n")
    wrapped = wrap_tool_with_security(web_fetch, AgentConfig(name="a"))
    out = wrapped.invoke({"url": "https://blocked.example/page"})
    assert "not in the allowlist" in out


# ── the dashboard seam: strict on save, round-trips ──────────────────────────


@pytest.fixture
def client(home):
    from unittest.mock import MagicMock

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    return TestClient(create_api(runtime))


def test_system_config_round_trips_and_normalizes(client):
    assert client.get("/api/system/config").json()["egress_allow"] == []
    saved = client.put(
        "/api/system/config",
        json={"egress_allow": ["https://API.GitHub.com/x", "*.example.com"], "egress_deny": []},
    )
    assert saved.status_code == 200, saved.text
    got = client.get("/api/system/config").json()
    assert got["egress_allow"] == ["api.github.com", "example.com"]
    assert got["egress_deny"] == []


def test_system_config_put_rejects_a_non_host_loudly(client):
    resp = client.put("/api/system/config", json={"egress_deny": ["not a host"]})
    assert resp.status_code == 400
    assert "egress_deny" in resp.json()["detail"]
    assert "not a host" in resp.json()["detail"]
    # Nothing was persisted.
    assert client.get("/api/system/config").json()["egress_deny"] == []


def test_system_config_put_requires_a_list(client):
    assert client.put("/api/system/config", json={"egress_allow": "x.com"}).status_code == 400


def test_saving_other_fields_leaves_the_lists_alone(client):
    client.put("/api/system/config", json={"egress_allow": ["keep.me"]})
    client.put("/api/system/config", json={"highlights_all_agents": True})
    assert client.get("/api/system/config").json()["egress_allow"] == ["keep.me"]


# ── the sandbox block's old fields: folded, and said so ──────────────────────


def test_builder_warns_that_sandbox_egress_fields_are_folded_not_a_firewall(caplog):
    from olano.config.backend_config import OlanoDockerBackendConfig
    from olano.core.builder import _warn_on_unimplemented_sandbox_fields

    cfg = OlanoDockerBackendConfig(egress_deny=["x.com"])
    with caplog.at_level(logging.WARNING):
        _warn_on_unimplemented_sandbox_fields(cfg, "agent-x")
    assert "egress_deny" in caplog.text
    assert "do NOT restrict the sandbox shell" in caplog.text
    assert "mounts" not in caplog.text  # not set, so not mentioned
