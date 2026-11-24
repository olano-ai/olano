"""Per-agent work-highlights generation + scoping, and weekly/monthly reports.

The daily highlights are generated once as a per-agent map so any view — a
member's granted subset, the whole fleet, a weekly/monthly rollup — is composed
from the same stored pieces (no re-generation, no cross-agent leak).
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, patch

from olano.analytics import journal, reports
from olano.config.home import (
    get_olano_paths,
    load_home_config,
    update_home_config,
)


def _fake_llm(reply: str):
    llm = AsyncMock()
    # _extract_text handles a plain string result (no .text()/.content attrs).
    llm.ainvoke = AsyncMock(return_value=reply)
    return llm


# ── parsing / composition ────────────────────────────────────────────────────


def test_parse_agent_highlights_splits_by_marker():
    text = (
        "LEAD: A productive day.\n"
        "[[agent:merli]]\nHandled **billing** questions.\n"
        "[[agent:scout]]\nRan a **scan**, all clear.\n"
    )
    lead, sections = journal._parse_agent_highlights(text)
    assert lead == "A productive day."
    assert sections == {
        "merli": "Handled **billing** questions.",
        "scout": "Ran a **scan**, all clear.",
    }


def test_parse_agent_highlights_no_markers_is_empty():
    lead, sections = journal._parse_agent_highlights("Just a plain paragraph, no markers.")
    assert sections == {}


def test_compose_and_scope_narrative():
    ah = {"merli": "Did A.", "scout": "Did B."}
    # Full fleet.
    full = journal.compose_narrative("Lead.", ah, None)
    assert "**merli** Did A." in full and "**scout** Did B." in full and full.startswith("Lead.")
    # Member scoped to merli only.
    scoped = journal.compose_narrative("Lead.", ah, {"merli"})
    assert "**merli**" in scoped and "scout" not in scoped
    # No matching agent → empty.
    assert journal.compose_narrative("Lead.", ah, {"ghost"}) == ""


def test_scoped_narrative_admin_vs_member_vs_legacy():
    entry = {
        "narrative": "FLEET",
        "narrative_lead": "Lead.",
        "agent_highlights": {"merli": "Did A.", "scout": "Did B."},
    }
    # Admin / show-all (agents=None) → the stored fleet narrative verbatim.
    assert journal.scoped_narrative(entry, None) == "FLEET"
    # Member → composed from only their agents.
    m = journal.scoped_narrative(entry, {"merli"})
    assert m and "merli" in m and "scout" not in m
    # Member with none of the agents → nothing.
    assert journal.scoped_narrative(entry, {"ghost"}) is None
    # Legacy entry (no per-agent map) can't be split → member gets nothing.
    legacy = {"narrative": "OLD FLEET TEXT", "agent_highlights": {}}
    assert journal.scoped_narrative(legacy, {"merli"}) is None
    assert journal.scoped_narrative(legacy, None) == "OLD FLEET TEXT"


def test_filter_work_scopes_all_signals():
    work = {
        "conversations": {"merli": [1], "scout": [2]},
        "scheduled_tasks": [{"agent": "merli"}, {"agent": "scout"}],
        "heartbeat": {"scout": [1]},
        "agent_messages": [
            {"from": "merli", "to": "ghost"},  # merli participates → kept
            {"from": "scout", "to": "ghost"},  # neither granted → dropped
        ],
        "cortex_groups": [{"agents": ["merli", "scout"], "cortex": [1]}],
    }
    out = journal.filter_work(work, {"merli"})
    assert list(out["conversations"]) == ["merli"]
    assert out["scheduled_tasks"] == [{"agent": "merli"}]
    assert out["heartbeat"] == {}
    assert out["agent_messages"] == [{"from": "merli", "to": "ghost"}]
    assert out["cortex_groups"] == [{"agents": ["merli"], "cortex": [1]}]


# ── generation stores the per-agent map ──────────────────────────────────────


async def test_generate_highlights_stores_per_agent_map():
    day = "2026-07-20"
    journal.build_journal_day(day, save=True)
    reply = "LEAD: Busy.\n[[agent:merli]]\nAnswered **billing**.\n[[agent:scout]]\nRan a scan.\n"
    with patch("olano.models.build_model", return_value=_fake_llm(reply)):
        entry = await journal.generate_highlights(day)
    assert entry["agent_highlights"] == {
        "merli": "Answered **billing**.",
        "scout": "Ran a scan.",
    }
    assert entry["narrative_lead"] == "Busy."
    assert "**merli**" in entry["narrative"] and "**scout**" in entry["narrative"]
    # Persisted + survives a metrics-only rebuild.
    rebuilt = journal.build_journal_day(day, save=True)
    assert rebuilt["agent_highlights"] == entry["agent_highlights"]


async def test_generate_highlights_without_markers_keeps_raw_text():
    day = "2026-07-19"
    journal.build_journal_day(day, save=True)
    with patch("olano.models.build_model", return_value=_fake_llm("A quiet day overall.")):
        entry = await journal.generate_highlights(day)
    assert entry["agent_highlights"] == {}
    assert entry["narrative"] == "A quiet day overall."
    # A member can't split a marker-less narrative → gets nothing (admins do).
    assert journal.scoped_narrative(entry, {"merli"}) is None


# ── reports ──────────────────────────────────────────────────────────────────


def test_period_key_and_bounds():
    anchor = date(2026, 7, 23)  # a Thursday in ISO week 30
    assert reports.period_key("weekly", anchor) == "2026-W30"
    assert reports.period_bounds("weekly", anchor) == (date(2026, 7, 20), date(2026, 7, 26))
    assert reports.period_key("monthly", anchor) == "2026-07"
    assert reports.period_bounds("monthly", anchor) == (date(2026, 7, 1), date(2026, 7, 31))


async def test_generate_report_rolls_up_daily_highlights():
    # Seed two days of per-agent daily highlights in the same ISO week.
    for day, reply in (
        ("2026-07-20", "LEAD: Mon.\n[[agent:merli]]\nMon work.\n"),
        ("2026-07-21", "LEAD: Tue.\n[[agent:scout]]\nTue scan.\n"),
    ):
        journal.build_journal_day(day, save=True)
        with patch("olano.models.build_model", return_value=_fake_llm(reply)):
            await journal.generate_highlights(day)

    # The rollup input must contain BOTH agents' daily lines.
    per_agent = reports.gather_range_highlights(date(2026, 7, 20), date(2026, 7, 26))
    assert set(per_agent) == {"merli", "scout"}

    rollup = "LEAD: Week.\n[[agent:merli]]\nBilling all week.\n[[agent:scout]]\nScans all week.\n"
    with patch("olano.models.build_model", return_value=_fake_llm(rollup)):
        rep = await reports.generate_report("weekly", "2026-07-23")
    assert rep["key"] == "2026-W30"
    assert set(rep["agent_highlights"]) == {"merli", "scout"}
    # Persisted + scopeable exactly like a daily entry.
    assert reports.read_report("weekly", "2026-W30") is not None
    assert "2026-W30" in reports.list_reports("weekly")
    member = journal.scoped_narrative(rep, {"merli"})
    assert member and "merli" in member and "scout" not in member


async def test_generate_report_quiet_period_no_llm_call():
    # No daily highlights anywhere → an empty report, no model call.
    with patch("olano.models.build_model") as bm:
        rep = await reports.generate_report("monthly", "2026-03-15")
    bm.assert_not_called()
    assert rep["agent_highlights"] == {}
    assert rep["narrative"] is None
    assert reports.read_report("monthly", "2026-03") is not None  # persisted


# ── config flag ──────────────────────────────────────────────────────────────


def test_highlights_all_agents_flag_roundtrip():
    assert load_home_config().highlights_all_agents is False  # default off
    update_home_config({"highlights_all_agents": True})
    assert load_home_config().highlights_all_agents is True


def test_reports_dir_path():
    assert get_olano_paths().reports_dir.name == "reports"
    assert get_olano_paths().reports_dir.parent.name == "journal"
