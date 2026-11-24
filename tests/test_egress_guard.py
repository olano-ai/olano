"""The egress lists bite on *every* outbound call, not just the gated tools.

``security/ssrf`` only runs in the handful of tools that call
``assert_safe_outbound_url``; the ~80 other modules that reach the network
build their own client. These tests pin that the chokepoints in
``security/egress_guard`` close that gap for all four libraries, and — just as
important — that they never touch traffic the platform itself makes.
"""

from __future__ import annotations

import asyncio
import urllib.error
import urllib.request

import aiohttp
import httpx
import pytest
from langchain_core.tools import tool

from olano.config.agent_config import AgentConfig
from olano.security import egress_guard
from olano.security.egress_guard import (
    INFRA_HOSTS,
    EgressBlockedError,
    active_policy,
    assert_url_allowed,
    check_host,
    check_host_against,
    exempt,
    governed,
)
from olano.security.pipeline import wrap_tool_with_security
from olano.security.request_context import push_request_context

BLOCKED = "https://evil.test/page"


@pytest.fixture(autouse=True)
def guard_installed():
    """The guard installs itself from ``egress.security_metadata`` on the first
    governed tool call; install it directly for the unit-level tests."""
    egress_guard.install()
    yield


@pytest.fixture
def policy():
    """A request context that looks like one governed tool call."""
    with push_request_context(
        metadata={"security": {"ssrf_allowlist": ["example.com"], "ssrf_blocklist": ["bad.test"]}}
    ):
        yield


# ── the decision ─────────────────────────────────────────────────────────────


def test_no_policy_means_no_enforcement():
    """Outside a governed tool call there is no policy, so the platform's own
    traffic — model turns, catalog sync, health reporting — is never touched."""
    assert active_policy() is None
    assert check_host("anything.test") is None


def test_allowed_denied_and_subdomains(policy):
    assert check_host("example.com") is None
    assert check_host("files.example.com") is None  # a domain covers subdomains
    assert check_host("evil.test") is not None
    assert "not in the allowlist" in check_host("evil.test")


def test_deny_beats_allow():
    with push_request_context(
        metadata={"security": {"ssrf_allowlist": ["bad.test"], "ssrf_blocklist": ["bad.test"]}}
    ):
        reason = check_host("bad.test")
        assert reason and "deny list" in reason


def test_check_host_against_an_explicit_policy_needs_no_request_context():
    """The browser evaluates requests on its own loop, where no tool's request
    context exists, against a policy it captured in the tool body."""
    assert active_policy() is None
    policy = (["example.com"], ["bad.test"])
    assert check_host_against("files.example.com", policy) is None
    assert "not in the allowlist" in (check_host_against("evil.test", policy) or "")
    assert "deny list" in (check_host_against("bad.test", (["bad.test"], ["bad.test"])) or "")
    assert check_host_against("evil.test", None) is None
    assert check_host_against("cloud.olano.ai", (["example.com"], [])) is None


def test_empty_policy_allows_everything():
    with push_request_context(metadata={"security": {"ssrf_allowlist": [], "ssrf_blocklist": []}}):
        assert active_policy() == ([], [])
        assert check_host("anything.test") is None


@pytest.mark.parametrize("host", ["olano.ai", "cloud.olano.ai", "auth.olano.ai", "x.olano.run"])
def test_infra_hosts_are_never_blockable(host):
    """A box that cannot reach its own control plane cannot be told to stop,
    cannot report that it is failing, and cannot be recovered without SSH."""
    with push_request_context(
        metadata={
            "security": {"ssrf_allowlist": ["example.com"], "ssrf_blocklist": list(INFRA_HOSTS)}
        }
    ):
        assert check_host(host) is None


def test_exempt_suppresses_and_governed_re_arms(policy):
    """`exempt` is safe to wrap a whole nested agent run: the model traffic is
    exempt, and that agent's own tools re-arm enforcement."""
    assert check_host("evil.test") is not None
    with exempt():
        assert check_host("evil.test") is None
        with governed():
            assert check_host("evil.test") is not None
        assert check_host("evil.test") is None
    assert check_host("evil.test") is not None


def test_unparseable_url_is_not_our_error(policy):
    """The client and the SSRF gate both reject a malformed URL with a better
    message than this layer could; it must not raise its own first."""
    assert_url_allowed("not a url")
    assert_url_allowed("")
    assert_url_allowed(None)


# ── the chokepoints ──────────────────────────────────────────────────────────


def test_httpx_sync_is_blocked(policy):
    with pytest.raises(EgressBlockedError):
        httpx.get(BLOCKED, timeout=1)


async def test_httpx_async_is_blocked(policy):
    async with httpx.AsyncClient() as client:
        with pytest.raises(EgressBlockedError):
            await client.get(BLOCKED, timeout=1)


def test_httpx_allowed_host_passes_through(policy):
    """The guard must not swallow the request it permits — proven against a
    mock transport so the test needs no network."""
    transport = httpx.MockTransport(lambda request: httpx.Response(200, text="ok"))
    with httpx.Client(transport=transport) as client:
        assert client.get("https://example.com/x").text == "ok"


async def test_aiohttp_is_blocked(policy):
    async with aiohttp.ClientSession() as session:
        with pytest.raises(EgressBlockedError):
            await session.get(BLOCKED)


def test_urllib_is_blocked(policy):
    with pytest.raises((EgressBlockedError, urllib.error.URLError)) as excinfo:
        urllib.request.urlopen(BLOCKED, timeout=1)
    # urllib wraps some exceptions in URLError; either way it must not connect.
    assert isinstance(excinfo.value, EgressBlockedError) or isinstance(
        getattr(excinfo.value, "reason", None), EgressBlockedError
    )


def test_urllib_accepts_a_request_object(policy):
    """`urlopen` takes a str or a Request; the guard has to read both."""
    with pytest.raises((EgressBlockedError, urllib.error.URLError)):
        urllib.request.urlopen(urllib.request.Request(BLOCKED), timeout=1)


# ── end to end through a tool that has no SSRF gate of its own ───────────────


def _bare_tool():
    @tool
    def bare_fetch(url: str) -> str:
        """A toolkit-style tool: a plain httpx call, no SSRF gate anywhere."""
        try:
            httpx.get(url, timeout=1)
            return "reached"
        except EgressBlockedError as exc:
            return f"Error: {exc}"
        except Exception as exc:  # any transport failure — it got past the guard
            return f"passed-guard:{type(exc).__name__}"

    return bare_fetch


def test_an_ungated_tool_is_now_governed(lionclaw_home):
    """The regression this exists for: before the guard, a toolkit that built
    its own client ignored the lists completely."""
    (lionclaw_home / "config.yaml").write_text("egress_allow: [example.com]\n")
    wrapped = wrap_tool_with_security(_bare_tool(), AgentConfig(name="a"))
    assert "not in the allowlist" in wrapped.invoke({"url": BLOCKED})
    # And the allowed host is not blocked by us (it fails or succeeds on the wire).
    assert "Error:" not in wrapped.invoke({"url": "https://example.com/"})


def test_the_same_tool_is_ungoverned_outside_a_tool_call(lionclaw_home):
    """Calling the underlying function directly is platform traffic, not an
    agent's tool call, and must not be policed."""
    (lionclaw_home / "config.yaml").write_text("egress_allow: [example.com]\n")
    assert "not in the allowlist" not in _bare_tool().invoke({"url": BLOCKED})


def test_a_nested_agents_tools_use_their_own_policy(lionclaw_home):
    """`push_request_context` merges, so without `governed()` a peer invoked
    mid-turn would inherit the CALLER's lists. Inside `exempt()` — which is how
    a nested run is wrapped — the nested tool must still be governed, by the
    policy its own wrapper pushes."""
    (lionclaw_home / "config.yaml").write_text("egress_allow: [example.com]\n")
    inner = wrap_tool_with_security(_bare_tool(), AgentConfig(name="inner"))
    with (
        push_request_context(
            metadata={"security": {"ssrf_allowlist": ["caller-only.test"], "ssrf_blocklist": []}}
        ),
        exempt(),
    ):
        # The caller's list would have blocked example.com; the inner agent's
        # own policy is what applies, and evil.test is still refused.
        assert "not in the allowlist" in inner.invoke({"url": BLOCKED})
        assert "Error:" not in inner.invoke({"url": "https://example.com/"})


# ── the browser lane (Chromium does not ride httpx) ──────────────────────────


@pytest.fixture
def browser_on(monkeypatch):
    """The browser tools exist only while config.yaml → browser turns them on."""
    from olano.browser import policy as browser_policy
    from olano.config.system_settings import set_browser_settings

    set_browser_settings({"mode": "headless"})
    monkeypatch.setattr(browser_policy, "_host", lambda: (4, 8 * browser_policy.GIB))


def test_browser_navigate_refuses_before_launching(policy, browser_on):
    """`browser_navigate` would otherwise be a hole straight through the
    allowlist — Chromium never touches the patched libraries."""
    from olano.tools.browser import browser_navigate

    out = browser_navigate.invoke({"url": BLOCKED})
    assert "not in the allowlist" in out


def test_browser_navigate_and_screenshot_refuses_before_launching(policy, browser_on):
    from olano.tools.browser import browser_navigate_and_screenshot

    out = browser_navigate_and_screenshot.invoke({"url": BLOCKED})
    assert "not in the allowlist" in out


# ── install / uninstall ──────────────────────────────────────────────────────


def test_install_is_idempotent():
    before = httpx.Client.send
    egress_guard.install()
    egress_guard.install()
    assert httpx.Client.send is before


def test_uninstall_restores_the_originals():
    egress_guard.install()
    patched = httpx.Client.send
    egress_guard.uninstall()
    assert httpx.Client.send is not patched
    egress_guard.install()  # leave it as the suite found it


def test_asyncio_task_inherits_the_policy(policy):
    """The contextvars must survive the hop into a task — a tool that fans out
    with `asyncio.gather` would otherwise escape the lists."""

    async def child():
        return check_host("evil.test")

    async def parent():
        return await asyncio.gather(child())

    assert asyncio.run(parent())[0] is not None
