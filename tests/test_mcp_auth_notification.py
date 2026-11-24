"""The bell row for an MCP server that needs a human to re-authorize it.

Everything upstream of this notification already retries — the SDK's token
refresh, the endpoint rediscovery a 404 triggers, the health sweep's reconnect
backoff — and none of them can invent consent. So when a grant is genuinely
spent, the *only* thing that moves it forward is a person clicking Authorize,
and until this producer existed the sole trace of that was a log line. The
visible symptom was an agent that quietly stopped having the tools it was
configured with, which reads as the model being lazy rather than as a
connection being down.

Network-free: the producer takes plain summary rows, which is the shape both
``builder.get_mcp_summary()`` and a health sweep's ``ServerReport.to_dict()``
already have.
"""

from __future__ import annotations

import pytest

from olano.runtime.server import _mcp_auth_key, _report_mcp_auth_required

AGENT = "maria"
URL = "https://mcp.stripe.com"


@pytest.fixture
def bell(monkeypatch) -> tuple[list[dict], list[str]]:
    """Capture what the producer would put on (and take off) the bell."""
    import olano.runtime.notifications as notifications

    raised: list[dict] = []
    resolved: list[str] = []
    monkeypatch.setattr(notifications, "notify", lambda **kw: raised.append(kw) or None)
    monkeypatch.setattr(notifications, "resolve", lambda key: resolved.append(key) or True)
    return raised, resolved


def _row(url: str = URL, *, ok: bool = False, status: str = "auth_required") -> dict:
    return {"url": url, "ok": ok, "status": status, "message": "OAuth token expired"}


def test_a_spent_grant_rings_with_the_panel_that_fixes_it(bell) -> None:
    raised, resolved = bell

    _report_mcp_auth_required(AGENT, [_row()])

    assert resolved == []
    assert len(raised) == 1
    row = raised[0]
    assert row["kind"] == "mcp_auth_required"
    assert row["agent"] == AGENT
    assert row["dedupe_key"] == f"mcp_auth_required:{AGENT}:{URL}"
    # The host, not the whole URL — the title is read at a glance in a panel
    # 360px wide.
    assert "mcp.stripe.com" in row["title"]
    # A row that names a problem and cannot open the fix is a dead end the
    # operator has to resolve by hand; every producer that can name a route
    # passes one.
    assert row["action"] == {"label": "Open MCP setup", "href": f"/agents/{AGENT}?tab=mcp"}


def test_the_body_says_why_reconnecting_will_not_help(bell) -> None:
    """The operator's first instinct is to hit Reload, which is exactly the
    thing that cannot work here. The row has to say so."""
    raised, _resolved = bell

    _report_mcp_auth_required(AGENT, [_row()])

    body = raised[0]["body"]
    assert URL in body
    assert "Authorize" in body


def test_a_connected_server_takes_the_row_back_down(bell) -> None:
    """The bell reflects the world, not the history."""
    raised, resolved = bell

    _report_mcp_auth_required(AGENT, [_row(ok=True, status="connected")])

    assert raised == []
    assert resolved == [_mcp_auth_key(AGENT, URL)]


@pytest.mark.parametrize("status", ["timeout", "error", "disabled", "misconfigured"])
def test_only_a_consent_problem_rings(bell, status: str) -> None:
    """A server that is down, disabled or misconfigured is not something to go
    authorize — and ringing for conditions that clear themselves trains the
    operator to ignore the bell."""
    raised, resolved = bell

    _report_mcp_auth_required(AGENT, [_row(status=status)])

    assert raised == []
    assert resolved == [_mcp_auth_key(AGENT, URL)]


def test_one_row_per_server_not_per_agent(bell) -> None:
    """Two dead grants are two things to go fix, in two different panels."""
    raised, _resolved = bell
    other = "https://mcp.notion.com"

    _report_mcp_auth_required(AGENT, [_row(), _row(other)])

    assert [r["dedupe_key"] for r in raised] == [
        _mcp_auth_key(AGENT, URL),
        _mcp_auth_key(AGENT, other),
    ]


def test_the_key_is_per_agent(bell) -> None:
    """Each agent authorizes the same server independently, so one agent's
    working grant must not silence another agent's dead one."""
    raised, _resolved = bell

    _report_mcp_auth_required("maria", [_row()])
    _report_mcp_auth_required("aurelius", [_row()])

    assert len({r["dedupe_key"] for r in raised}) == 2


def test_a_queued_reconnect_stays_quiet_this_tick(bell) -> None:
    """The sweep only queues a reconnect for an `auth_required` server once its
    token file says it has a usable grant again — i.e. someone just authorized
    it and the build summary is stale. Ringing then would be wrong by the time
    the operator read it; if the reconnect fails, the next tick rings with
    nothing pending."""
    raised, resolved = bell

    _report_mcp_auth_required(AGENT, [{**_row(), "action": "reload_requested"}])

    assert raised == []
    assert resolved == []


def test_a_server_waiting_out_its_backoff_still_rings(bell) -> None:
    """`backoff` means the sweep already tried and is now waiting up to six
    hours. That is precisely when the operator needs to know."""
    raised, _resolved = bell

    _report_mcp_auth_required(AGENT, [{**_row(), "action": "backoff"}])

    assert len(raised) == 1


def test_a_row_without_a_url_is_skipped(bell) -> None:
    """A stdio server with no url would otherwise dedupe onto one blank key."""
    raised, resolved = bell

    _report_mcp_auth_required(AGENT, [{"ok": False, "status": "auth_required"}])

    assert raised == []
    assert resolved == []


def test_a_broken_bell_never_breaks_the_boot(monkeypatch) -> None:
    """This runs inline in the agent boot path and in the health sweep. A
    notification that took either of those down would be far worse than no
    notification."""
    import olano.runtime.notifications as notifications

    def _explode(**_kw):
        raise RuntimeError("notifications store is unwritable")

    monkeypatch.setattr(notifications, "notify", _explode)

    _report_mcp_auth_required(AGENT, [_row()])  # must not raise


# ── Alerts that outlive their server ─────────────────────────────────────────
#
# These run against the real store (the autouse home fixture gives each test
# its own), because the bug was a row nobody ever took back down: a fake
# `resolve` cannot show what is left on the bell.


@pytest.fixture
def store():
    import olano.runtime.notifications as notifications

    notifications.reset_for_tests()
    return notifications


def _keys(notifications) -> set[str]:
    return {n["dedupe_key"] for n in notifications.list_notifications()}


def test_a_disabled_server_stops_ringing_even_with_a_stale_summary(store) -> None:
    """The sweep reads `enabled` off the config, which can be newer than the
    build summary that still says `auth_required`."""
    _report_mcp_auth_required(AGENT, [_row()])
    assert _keys(store) == {_mcp_auth_key(AGENT, URL)}

    _report_mcp_auth_required(AGENT, [{**_row(), "enabled": False}])

    assert _keys(store) == set()


def test_a_server_removed_from_the_config_takes_its_alert_with_it(store) -> None:
    other = "https://mcp.linear.app/mcp"
    _report_mcp_auth_required(AGENT, [_row(), _row(other)])
    assert len(_keys(store)) == 2

    # `other` was removed: the next report has no row for it at all.
    _report_mcp_auth_required(AGENT, [_row()])

    assert _keys(store) == {_mcp_auth_key(AGENT, URL)}


def test_an_agent_with_no_servers_left_clears_all_of_its_alerts(store) -> None:
    _report_mcp_auth_required(AGENT, [_row()])
    _report_mcp_auth_required("aurelius", [_row()])

    _report_mcp_auth_required(AGENT, [])

    # Another agent's alert for the same server is its own problem.
    assert _keys(store) == {_mcp_auth_key("aurelius", URL)}


def test_pruning_matches_the_agent_exactly_not_by_prefix(store) -> None:
    """`maria` must not clear `maria2`'s alerts."""
    _report_mcp_auth_required("maria2", [_row()])

    _report_mcp_auth_required("maria", [])

    assert _keys(store) == {_mcp_auth_key("maria2", URL)}


def test_deleting_an_agent_clears_its_bell(store) -> None:
    _report_mcp_auth_required(AGENT, [_row()])
    _report_mcp_auth_required("aurelius", [_row()])

    assert store.forget_agent(AGENT) == 1

    assert _keys(store) == {_mcp_auth_key("aurelius", URL)}


def test_the_sweep_prunes_alerts_for_agents_that_no_longer_exist(store, monkeypatch) -> None:
    """A row raised before deletes cleared the bell (or by a directory removed
    by hand) has no producer left to resolve it."""
    import olano.config.loader as loader
    from olano.runtime.server import _prune_orphaned_mcp_auth_alerts

    _report_mcp_auth_required("ghost", [_row()])
    _report_mcp_auth_required("alive", [_row()])
    store.notify(kind="other", title="unrelated", agent="ghost", dedupe_key="other:ghost")
    monkeypatch.setattr(
        loader,
        "find_agent_config_path",
        lambda name, **_kw: object() if name == "alive" else None,
    )

    assert _prune_orphaned_mcp_auth_alerts() == 1

    assert _keys(store) == {_mcp_auth_key("alive", URL), "other:ghost"}
