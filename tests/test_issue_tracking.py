"""Issue tracker: store semantics, tool surface, attribution binding."""

from __future__ import annotations

import pytest

from olano.issues.store import IssueError, get_issue_store
from olano.tools import issue_tools
from olano.tools.talk_to import current_agent


@pytest.fixture
def as_agent():
    def _bind(name: str):
        return current_agent.set(name)

    token = current_agent.set("scout")
    yield _bind
    current_agent.reset(token)


class TestIssueStore:
    def test_create_and_get(self):
        store = get_issue_store()
        issue = store.create(title="Broken feed", body="details", author="scout")
        assert issue["state"] == "open"
        assert issue["author"] == "scout"
        assert store.get(issue["id"]) == issue

    def test_empty_title_rejected(self):
        with pytest.raises(IssueError):
            get_issue_store().create(title="  ", body="", author="scout")

    def test_close_reopen_transitions(self):
        store = get_issue_store()
        issue = store.create(title="t", body="", author="a")
        closed = store.set_state(issue["id"], actor="b", state="closed", reason="fixed")
        assert closed["state"] == "closed"
        assert closed["closed_at"]
        with pytest.raises(IssueError):  # double-close is an error, not a no-op
            store.set_state(issue["id"], actor="b", state="closed")
        reopened = store.set_state(issue["id"], actor="c", state="open")
        assert reopened["state"] == "open"
        assert reopened["closed_at"] is None
        kinds = [e["kind"] for e in store.events(issue["id"])]
        assert kinds == ["close", "reopen"]

    def test_events_carry_actor_and_payload(self):
        store = get_issue_store()
        issue = store.create(title="t", body="", author="a", assignee="merli")
        store.comment(issue["id"], actor="merli", body="on it")
        events = store.events(issue["id"])
        assert events[0]["kind"] == "assign"
        assert events[0]["payload"] == {"assignee": "merli"}
        assert events[1]["actor"] == "merli"
        assert events[1]["payload"]["body"] == "on it"

    def test_list_filters(self):
        store = get_issue_store()
        a = store.create(title="one", body="", author="x", assignee="merli", labels=["bug"])
        store.create(title="two", body="", author="y")
        store.set_state(a["id"], actor="x", state="closed")
        assert [i["title"] for i in store.list_issues(state="open")] == ["two"]
        assert [i["title"] for i in store.list_issues(state="closed")] == ["one"]
        assert [i["title"] for i in store.list_issues(state="", label="bug")] == ["one"]
        assert [i["title"] for i in store.list_issues(state="", assignee="merli")] == ["one"]

    def test_unknown_issue_raises(self):
        with pytest.raises(IssueError):
            get_issue_store().comment(999, actor="a", body="x")


class TestIssueTools:
    def test_author_binds_from_current_agent(self, as_agent):
        out = issue_tools.issue_create.func(title="From scout")
        assert "author: ai-scout" in out
        as_agent("merli")
        out = issue_tools.issue_comment.func(issue_id=1, body="ack")
        events = get_issue_store().events(1)
        assert events[-1]["actor"] == "ai-merli"

    def test_unbound_actor_is_system(self):
        token = current_agent.set("")
        try:
            issue_tools.issue_create.func(title="From nobody")
            assert get_issue_store().list_issues(state="open")[0]["author"] == "system"
        finally:
            current_agent.reset(token)

    def test_full_lifecycle_via_tools(self, as_agent):
        assert "Created #1" in issue_tools.issue_create.func(title="bug", labels="a, b")
        assert "Closed" in issue_tools.issue_close.func(issue_id=1, reason="done")
        assert "Reopened" in issue_tools.issue_reopen.func(issue_id=1)
        assert "already open" in issue_tools.issue_reopen.func(issue_id=1)
        shown = issue_tools.issue_show.func(issue_id=1)
        assert "close (done)" in shown and "reopen" in shown
        assert "#1" in issue_tools.issue_list.func()

    def test_unknown_assignee_rejected_with_roster(self, as_agent, monkeypatch):
        monkeypatch.setattr("olano.issues.principals._agent_roster", lambda: ["merli", "scout"])
        monkeypatch.setattr("olano.issues.principals._human_roster", list)
        out = issue_tools.issue_create.func(title="t", assignee="nosuch")
        assert out.startswith("[tool-error]") and "ai-merli" in out
        # Case-insensitive bare match canonicalizes to the ai- form
        out = issue_tools.issue_create.func(title="t", assignee="MERLI")
        assert "assignee: ai-merli" in out

    def test_toolkit_registered(self):
        from olano.tools.registry import resolve_tools

        names = {t.name for t in resolve_tools(["issue_create", "issue_list", "issue_show"])}
        assert names == {"issue_create", "issue_list", "issue_show"}

    def test_errors_never_raise(self, as_agent):
        assert issue_tools.issue_close.func(issue_id=12345).startswith("[tool-error]")
        assert issue_tools.issue_comment.func(issue_id=1, body="").startswith("[tool-error]")


class TestPrincipals:
    """The naming decision: ai-/human- canonical forms, bare-name resolution."""

    @pytest.fixture(autouse=True)
    def rosters(self, monkeypatch):
        monkeypatch.setattr("olano.issues.principals._agent_roster", lambda: ["carlos", "merli"])
        monkeypatch.setattr("olano.issues.principals._human_roster", lambda: ["carlos", "dana"])

    def test_bare_name_unique_resolves(self):
        from olano.issues.principals import resolve_assignee

        assert resolve_assignee("merli") == ("ai-merli", None)
        assert resolve_assignee("dana") == ("human-dana", None)

    def test_shared_name_is_ambiguous_with_both_options(self):
        from olano.issues.principals import resolve_assignee

        canonical, err = resolve_assignee("carlos")
        assert canonical == "" and err is not None
        assert "ai-carlos" in err and "human-carlos" in err

    def test_canonical_forms_always_resolve(self):
        from olano.issues.principals import resolve_assignee

        assert resolve_assignee("ai-carlos") == ("ai-carlos", None)
        assert resolve_assignee("human-carlos") == ("human-carlos", None)
        assert resolve_assignee("AI-CARLOS") == ("ai-carlos", None)

    def test_system_reserved(self):
        from olano.issues.principals import resolve_assignee

        canonical, err = resolve_assignee("system")
        assert canonical == "" and "reserved" in (err or "")

    def test_empty_is_unassigned(self):
        from olano.issues.principals import resolve_assignee

        assert resolve_assignee("") == ("", None)

    def test_slugify_mints_handles(self):
        """`slugify` mints a handle; it is no longer on the resolution path."""
        from olano.issues.principals import slugify

        assert slugify("Carlos M.") == "carlos-m"
        assert slugify("") == "user"

    def test_human_principal_is_a_pure_prefix(self):
        """It takes a stored handle, never a display name.

        The old contract slugified its argument, which made the principal a
        function of a mutable field — renaming a user re-keyed their identity
        in an append-only audit log.
        """
        from olano.issues.principals import human_principal

        assert human_principal("owner") == "human-owner"
        assert human_principal("carlos-m-2") == "human-carlos-m-2"

    def test_kind_and_bare_name(self):
        from olano.issues.principals import bare_name, kind

        assert kind("ai-merli") == "agent" and bare_name("ai-merli") == "merli"
        assert kind("human-dana") == "human" and bare_name("human-dana") == "dana"
        assert kind("system") == "system"


class TestIssueType:
    def test_type_stored_and_filterable(self):
        store = get_issue_store()
        store.create(title="a bug", body="", author="ai-x", issue_type="bug")
        store.create(title="a task", body="", author="ai-x")
        assert [i["title"] for i in store.list_issues(issue_type="bug")] == ["a bug"]
        assert store.list_issues(issue_type="task")[0]["type"] == "task"

    def test_invalid_type_rejected(self):
        with pytest.raises(IssueError):
            get_issue_store().create(title="t", body="", author="a", issue_type="epic")

    def test_tool_passes_type(self, as_agent):
        out = issue_tools.issue_create.func(title="broken", issue_type="bug")
        assert "[bug]" in out


class TestHumanDelivery:
    def test_assign_to_human_notifies(self, as_agent, monkeypatch):
        calls = []
        monkeypatch.setattr("olano.issues.principals._agent_roster", lambda: ["scout"])
        monkeypatch.setattr("olano.issues.principals._human_roster", lambda: ["carlos"])
        import olano.runtime.notifications as notifications

        monkeypatch.setattr(notifications, "notify", lambda **kw: calls.append(kw) or None)
        out = issue_tools.issue_create.func(title="for carlos", assignee="carlos")
        assert "assignee: human-carlos" in out
        assert len(calls) == 1
        assert "human-carlos" not in calls[0]["title"]  # bare name in UI copy
        assert calls[0]["kind"] == "issue_assigned"

    def test_the_notification_opens_the_issue_it_names(self, as_agent, monkeypatch):
        """A row saying "Issue #10 assigned to you" is only useful if it opens
        issue #10 — the dashboard addresses one issue's page with ?issue=N."""
        calls = []
        monkeypatch.setattr("olano.issues.principals._agent_roster", lambda: ["scout"])
        monkeypatch.setattr("olano.issues.principals._human_roster", lambda: ["carlos"])
        import olano.runtime.notifications as notifications

        monkeypatch.setattr(notifications, "notify", lambda **kw: calls.append(kw) or None)
        issue_tools.issue_create.func(title="for carlos", assignee="carlos")

        issue_id = get_issue_store().list_issues(state="open", assignee="human-carlos")[0]["id"]
        assert calls[0]["action"] == {"label": "Open issue", "href": f"/issues?issue={issue_id}"}

    def test_assign_to_agent_does_not_notify(self, as_agent, monkeypatch):
        calls = []
        monkeypatch.setattr("olano.issues.principals._agent_roster", lambda: ["merli"])
        monkeypatch.setattr("olano.issues.principals._human_roster", list)
        import olano.runtime.notifications as notifications

        monkeypatch.setattr(notifications, "notify", lambda **kw: calls.append(kw) or None)
        issue_tools.issue_create.func(title="for merli", assignee="merli")
        assert calls == []


class TestIssuePoll:
    """Dedicated delivery poll — decoupled from the heartbeat by design."""

    def test_watermark_roundtrip(self):
        store = get_issue_store()
        assert store.get_watermark("ai-merli") == ""
        store.set_watermark("ai-merli", "2026-08-09T00:00:00+00:00")
        assert store.get_watermark("ai-merli") == "2026-08-09T00:00:00+00:00"
        store.set_watermark("ai-merli")  # no arg -> now
        assert store.get_watermark("ai-merli") > "2026-08-09T00:00:00+00:00"

    def test_pending_respects_watermark(self):
        from olano.issues.delivery import mark_checked, pending_for_agent

        store = get_issue_store()
        store.create(title="one", body="", author="ai-scout", assignee="ai-merli")
        assert [i["title"] for i in pending_for_agent("merli")] == ["one"]
        mark_checked("merli")
        assert pending_for_agent("merli") == []
        # New activity bumps updated_at -> pending again
        store.comment(1, actor="ai-scout", body="ping")
        assert [i["title"] for i in pending_for_agent("merli")] == ["one"]

    def test_closed_issues_never_pending(self):
        from olano.issues.delivery import pending_for_agent

        store = get_issue_store()
        issue = store.create(title="done", body="", author="a", assignee="ai-merli")
        store.set_state(issue["id"], actor="a", state="closed")
        assert pending_for_agent("merli") == []

    def test_effective_settings_resolution(self, base_config):
        from olano.config.system_settings import (
            IssuesSettings,
            effective_issues_auto_process,
            effective_issues_poll_minutes,
        )

        # Defaults: on, 15 min
        assert effective_issues_auto_process(base_config, settings=IssuesSettings()) is True
        assert effective_issues_poll_minutes(base_config, settings=IssuesSettings()) == 15
        # Per-agent override wins over the home-wide default
        base_config.issues.auto_process = False
        base_config.issues.poll_minutes = 5
        assert effective_issues_auto_process(base_config, settings=IssuesSettings()) is False
        assert effective_issues_poll_minutes(base_config, settings=IssuesSettings()) == 5

    def test_poll_message_lists_issues(self):
        from olano.issues.delivery import format_poll_message

        msg = format_poll_message(
            [{"id": 3, "type": "bug", "title": "broken feed", "author": "human-owner"}]
        )
        assert "#3" in msg and "[bug]" in msg and "human-owner" in msg
        assert "issue_show" in msg and "issue_close" in msg


class TestIssuePollJob:
    async def test_poll_runs_turn_and_advances_watermark(self, base_config, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch

        from olano.issues.delivery import pending_for_agent
        from olano.scheduler.cron import OlanoScheduler

        base_config.name = "merli"
        store = get_issue_store()
        store.create(title="fix the feed", body="", author="human-owner", assignee="ai-merli")

        runner = MagicMock()
        runner.ainvoke = AsyncMock(return_value="on it")
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched._add_issues_job(base_config, runner)
        poll = add_job.call_args.args[0]

        await poll()
        runner.ainvoke.assert_awaited_once()
        message = runner.ainvoke.await_args.args[0]
        assert "ISSUE TRACKER" in message and "fix the feed" in message
        # Watermark advanced -> second poll is silent
        assert pending_for_agent("merli") == []
        await poll()
        assert runner.ainvoke.await_count == 1

    async def test_manual_mode_never_polls(self, base_config, monkeypatch):
        from unittest.mock import AsyncMock, MagicMock, patch

        from olano.scheduler.cron import OlanoScheduler

        base_config.name = "merli"
        base_config.issues.auto_process = False  # manual mode
        get_issue_store().create(title="waiting", body="", author="a", assignee="ai-merli")

        runner = MagicMock()
        runner.ainvoke = AsyncMock()
        sched = OlanoScheduler()
        with patch.object(sched._scheduler, "add_job") as add_job:
            sched._add_issues_job(base_config, runner)
        await add_job.call_args.args[0]()
        runner.ainvoke.assert_not_awaited()
        # The issue is still pending for whenever a human prompts the agent.
        from olano.issues.delivery import pending_for_agent

        assert [i["title"] for i in pending_for_agent("merli")] == ["waiting"]


class TestSearchSortAndFieldEdits:
    def test_query_searches_title_and_body(self):
        store = get_issue_store()
        store.create(title="feed importer broken", body="", author="a")
        store.create(title="other", body="mentions the feed too", author="a")
        store.create(title="unrelated", body="nothing", author="a")
        hits = store.list_issues(query="feed")
        assert {i["title"] for i in hits} == {"feed importer broken", "other"}

    def test_sort_orders(self):
        store = get_issue_store()
        a = store.create(title="first", body="", author="a")
        store.create(title="second", body="", author="a")
        store.comment(a["id"], actor="a", body="bump")  # first is now most recent
        assert next(i["title"] for i in store.list_issues(sort="updated")) == "first"
        assert [i["title"] for i in store.list_issues(sort="id")] == ["first", "second"]
        assert next(i["title"] for i in store.list_issues(sort="oldest")) == "first"

    def test_set_labels_replaces_and_logs(self):
        store = get_issue_store()
        issue = store.create(title="t", body="", author="a", labels=["old"])
        updated = store.set_labels(issue["id"], actor="human-owner", labels=["bug", "feeds", "bug"])
        assert updated["labels"] == ["bug", "feeds"]  # deduped, sorted
        event = store.events(issue["id"])[-1]
        assert event["kind"] == "label" and event["payload"]["labels"] == ["bug", "feeds"]

    def test_set_type_validates_and_logs(self):
        store = get_issue_store()
        issue = store.create(title="t", body="", author="a")
        updated = store.set_type(issue["id"], actor="human-owner", issue_type="bug")
        assert updated["type"] == "bug"
        assert store.events(issue["id"])[-1]["kind"] == "type"
        with pytest.raises(IssueError):
            store.set_type(issue["id"], actor="x", issue_type="epic")


class TestUniversalToolInjection:
    def test_issue_tools_injected_into_every_agent(self, base_config):
        # base_config declares no tools — the universal set must include the
        # issue tools so the poll's delivery prompt never references tools
        # the agent doesn't have.
        for name in ("issue_create", "issue_list", "issue_show", "issue_close"):
            assert name in base_config.tools


class TestCommentCount:
    def test_list_rows_carry_comment_count(self):
        store = get_issue_store()
        issue = store.create(title="t", body="", author="a")
        store.create(title="quiet", body="", author="a")
        store.comment(issue["id"], actor="b", body="one")
        store.comment(issue["id"], actor="c", body="two")
        rows = {i["title"]: i for i in store.list_issues(state="open")}
        assert rows["t"]["comment_count"] == 2
        assert rows["quiet"]["comment_count"] == 0


class TestKeywordSearchFTS:
    """Slice 1: FTS5 over title + body + comments, BM25-ranked."""

    def test_search_matches_comments(self):
        store = get_issue_store()
        a = store.create(title="unrelated title", body="nothing here", author="x")
        store.create(title="also unrelated", body="quiet", author="x")
        store.comment(a["id"], actor="x", body="the webhook signature fails on retry")
        hits = store.list_issues(query="webhook signature")
        assert [i["id"] for i in hits] == [a["id"]]

    def test_relevance_ranks_title_match_first(self):
        store = get_issue_store()
        body_hit = store.create(title="misc", body="mentions billing once", author="x")
        title_hit = store.create(title="billing broken on checkout billing", body="", author="x")
        hits = store.list_issues(query="billing", sort="relevance")
        assert hits[0]["id"] == title_hit["id"]
        assert {i["id"] for i in hits} == {title_hit["id"], body_hit["id"]}

    def test_punctuation_queries_never_raise(self):
        store = get_issue_store()
        store.create(title="openjobs.bot importer", body="", author="x")
        # These are FTS5 syntax errors when passed raw — the sanitizer quotes them.
        assert store.list_issues(query="openjobs.bot") != []
        assert store.list_issues(query='weird "quotes" (and) *stars*') == []
        assert store.list_issues(query="...") == []

    def test_filters_compose_with_search(self):
        store = get_issue_store()
        a = store.create(title="feed bug", body="", author="x", issue_type="bug")
        store.create(title="feed feature", body="", author="x", issue_type="feature")
        hits = store.list_issues(query="feed", issue_type="bug")
        assert [i["id"] for i in hits] == [a["id"]]

    def test_like_fallback_when_fts_unavailable(self):
        store = get_issue_store()
        store.create(title="fallback probe", body="", author="x")
        store._fts_ok = False
        assert [i["title"] for i in store.list_issues(query="fallback")] == ["fallback probe"]
        assert store.search_ids("fallback") is None


class TestSemanticSearch:
    """Slice 2: stubbed embedder — deterministic vectors, no model download."""

    @pytest.fixture(autouse=True)
    def stub_embedder(self, monkeypatch):
        import numpy as np

        from olano.issues import semantic

        # Toy semantic space: axis 0 = "payments", axis 1 = "feeds", axis 2 = rest.
        def fake_embed(texts):
            vecs = []
            for text in texts:
                lower = text.lower()
                v = np.zeros(3, dtype=np.float32)
                if any(w in lower for w in ("billing", "payment", "checkout", "invoice")):
                    v[0] = 1.0
                if any(w in lower for w in ("feed", "rss", "importer")):
                    v[1] = 1.0
                if not v.any():
                    v[2] = 1.0
                vecs.append(v / np.linalg.norm(v))
            return np.stack(vecs)

        monkeypatch.setattr(semantic, "_embed_texts", fake_embed)
        monkeypatch.setattr(semantic, "_model_name", lambda: "stub-model")
        monkeypatch.setenv("LIONCLAW_SYNC_ISSUE_EMBED", "1")
        return fake_embed

    def test_index_and_similar(self):
        from olano.issues import semantic

        store = get_issue_store()
        pay = store.create(title="billing fails at checkout", body="", author="x")
        store.create(title="rss feed drops items", body="", author="x")
        assert semantic.index_pending() == 2
        sims = semantic.similar_issues("payment invoice problem")
        assert sims and sims[0]["id"] == pay["id"]
        assert sims[0]["score"] > semantic.DUPLICATE_THRESHOLD

    def test_hybrid_fuses_keyword_and_vector(self):
        from olano.issues import semantic

        store = get_issue_store()
        # Semantic-only match: no shared keywords with the query.
        sem = store.create(title="invoice charge declined", body="", author="x")
        # Keyword-only match: contains the literal word but lives in "rest" space.
        kw = store.create(title="the word payment appears in docs task", body="", author="x")
        semantic.index_pending()
        ranked = semantic.hybrid_ranked_ids("payment failing")
        assert ranked is not None and set(ranked) >= {sem["id"], kw["id"]}

    def test_duplicate_check_blocks_and_allows(self, as_agent):
        from olano.issues import semantic

        store = get_issue_store()
        store.create(title="billing error on checkout", body="", author="ai-scout")
        semantic.index_pending()
        out = issue_tools.issue_create.func(title="payment failing at checkout")
        assert out.startswith("[duplicate-check]") and "#1" in out
        assert "allow_duplicate" in out
        out = issue_tools.issue_create.func(
            title="payment failing at checkout", allow_duplicate=True
        )
        assert "Created #2" in out

    def test_closed_issues_never_block_creation(self, as_agent):
        from olano.issues import semantic

        store = get_issue_store()
        old = store.create(title="billing error on checkout", body="", author="ai-scout")
        store.set_state(old["id"], actor="ai-scout", state="closed")
        semantic.index_pending()
        out = issue_tools.issue_create.func(title="payment failing at checkout")
        assert "Created #2" in out

    def test_tool_search_uses_hybrid(self, as_agent):
        from olano.issues import semantic

        store = get_issue_store()
        sem = store.create(title="invoice charge declined", body="", author="x")
        semantic.index_pending()
        out = issue_tools.issue_list.func(query="payment failing")
        assert f"#{sem['id']}" in out

    def test_degrades_silently_without_embedder(self, monkeypatch, as_agent):
        from olano.issues import semantic

        monkeypatch.setattr(semantic, "_embed_texts", lambda texts: None)
        store = get_issue_store()
        store.create(title="keyword probe", body="", author="x")
        assert semantic.similar_issues("anything") == []
        assert semantic.index_pending() == 0
        # Keyword search still works end to end.
        assert "keyword probe" in issue_tools.issue_list.func(query="probe")
        # And creation is never blocked.
        assert "Created" in issue_tools.issue_create.func(title="keyword probe two")
