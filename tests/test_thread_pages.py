"""A conversation read a page at a time, with or without tool output.

The mobile app opens a long conversation on its newest page and fetches older
ones as the person scrolls up, and it draws tool steps but never their output.
These pin the contract it relies on: pages join back into the whole
transcript, never split a turn, and a read without the new parameters is the
response it always was.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from olano.runtime import thread_pages


@pytest.fixture
def api_client(tmp_workspace, monkeypatch):
    monkeypatch.setenv("LIONCLAW_WORKSPACE", str(tmp_workspace))
    monkeypatch.setenv("LIONCLAW_HOME", str(tmp_workspace / "home"))
    from fastapi.testclient import TestClient

    from olano.runtime.api import create_api

    runtime = MagicMock()
    runtime._runners = {}
    runtime._channels = []
    return TestClient(create_api(runtime))


def _turn(i: int, steps: int) -> list[dict]:
    rows = [{"ts": f"2026-10-01T00:{i:02d}:00Z", "role": "user", "content": f"question {i}"}]
    for k in range(steps):
        rows.append(
            {
                "ts": "t",
                "role": "tool_call",
                "content": json.dumps({"path": f"/f{k}"}),
                "name": "read_file",
            }
        )
        rows.append(
            {
                "ts": "t",
                "role": "tool_result",
                "content": f"read_file: contents {i}.{k} " + "x" * 300,
            }
        )
    rows.append({"ts": "t", "role": "assistant", "content": f"answer {i}"})
    return rows


def _write_thread(rows: list[dict], thread_id: str = "long") -> str:
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "aurelius"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / f"{thread_id}.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )
    return f"/api/agents/aurelius/threads/{thread_id}"


def _rows() -> list[dict]:
    rows = [{"ts": "t", "role": "system", "content": "prompt changed"}]
    for i in range(12):
        rows += _turn(i, steps=i % 4)
        if i == 5:
            # Hidden and invisible rows take no position.
            rows.append({"ts": "t", "role": "user", "content": "INTERNAL", "hidden": True})
            rows.append({"ts": "t", "role": "system", "content": "not drawn"})
        if i == 7:
            # A message steered into the running turn does not open a new one.
            rows.insert(-1, {"ts": "t", "role": "user", "content": "also check x", "steer": True})
    return rows


def test_a_read_without_parameters_is_the_whole_transcript(api_client):
    url = _write_thread(_rows())
    body = api_client.get(url).json()
    assert "start" not in body and "has_more" not in body
    assert body["messages"][0]["content"] == "question 0"
    assert all("output_omitted" not in m for m in body["messages"])


def test_pages_join_back_into_the_whole_transcript_and_never_split_a_turn(api_client):
    url = _write_thread(_rows())
    whole = api_client.get(url).json()["messages"]

    pages, before = [], None
    while True:
        query = "?limit=7" + (f"&before={before}" if before is not None else "")
        body = api_client.get(url + query).json()
        pages.append(body["messages"])
        if body["start"] > 0:
            first = body["messages"][0]
            assert first["role"] == "user" and not first["steer"], first
        if not body["has_more"]:
            assert body["start"] == 0
            break
        assert before is None or body["start"] < before
        before = body["start"]

    assert len(pages) > 2
    assert [m for page in reversed(pages) for m in page] == whole


def test_the_newest_page_still_carries_what_opening_a_conversation_needs(api_client):
    rows = _rows()
    rows[-1]["model"] = "olano:pro"
    url = _write_thread(rows)
    body = api_client.get(url + "?limit=5").json()
    assert body["messages"][-1]["content"] == "answer 11"
    assert body["model"] == "olano:pro"
    assert "run" in body
    # The header's count is the conversation's, not the page's.
    assert body["tool_calls"] == sum(1 for r in rows if r["role"] == "tool_call")


def test_tool_output_is_left_out_except_where_the_chat_reads_it(api_client):
    magic = json.dumps({"url": "https://olano.example/m/abc", "label": "Connect Google"})
    oauth = json.dumps({"authorization_url": "https://auth.example/authorize?x=1"})
    rows = [
        {"ts": "t", "role": "user", "content": "connect things"},
        {"ts": "t", "role": "tool_call", "content": "{}", "name": "read_file"},
        {"ts": "t", "role": "tool_result", "content": "read_file: " + "secret-ish file body " * 50},
        {"ts": "t", "role": "tool_result", "content": "lookup", "name": "web_search"},
        {"ts": "t", "role": "tool_result", "content": f"magic_link_connect_service: {magic}"},
        {"ts": "t", "role": "tool_result", "content": f"self_authorize_mcp_server: {oauth}"},
        {"ts": "t", "role": "assistant", "content": "done"},
    ]
    url = _write_thread(rows)
    msgs = api_client.get(url + "?tool_results=status").json()["messages"]
    results = [m for m in msgs if m["role"] == "tool_result"]
    assert results[0]["content"] == "read_file: " and results[0]["output_omitted"] is True
    assert results[1]["content"] == "" and results[1]["name"] == "web_search"
    assert results[2]["content"] == f"magic_link_connect_service: {magic}"
    assert results[3]["content"] == f"self_authorize_mcp_server: {oauth}"
    assert "output_omitted" not in results[2] and "output_omitted" not in results[3]
    # Everything that is not a tool result is untouched.
    full = api_client.get(url).json()["messages"]
    assert [m for m in msgs if m["role"] != "tool_result"] == [
        m for m in full if m["role"] != "tool_result"
    ]


def test_bad_parameters_are_refused(api_client):
    url = _write_thread(_rows())
    assert api_client.get(url + "?limit=0").status_code == 422
    assert api_client.get(url + "?before=-1").status_code == 422
    assert api_client.get(url + "?tool_results=everything").status_code == 422


def test_a_turn_longer_than_the_page_comes_whole():
    rows = (
        [{"role": "user"}]
        + [{"role": "tool_call"}, {"role": "tool_result"}] * 20
        + [{"role": "assistant"}]
    )
    rows += [{"role": "user"}, {"role": "assistant"}]
    # A page that reaches into the long turn takes all of it...
    assert thread_pages.page_bounds(rows, 5, None) == (0, len(rows))
    # ...and one that does not stops at the turn before it.
    assert thread_pages.page_bounds(rows, 2, None) == (len(rows) - 2, len(rows))
    assert thread_pages.page_bounds(rows, 5, len(rows) - 2) == (0, len(rows) - 2)


# ── The thread list, a page at a time ────────────────────────────────────────


def _seed_threads(count: int) -> list[str]:
    """``count`` threads, newest last, each with its own mtime: every fourth is
    a scheduled run and every sixth an agent-to-agent thread."""
    import os
    import time

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "aurelius"
    sessions.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    base = time.time() - count * 10
    for i in range(count):
        stamp = base + i * 10
        ts = datetime.fromtimestamp(stamp, timezone.utc).isoformat()
        if i % 4 == 3:
            tid = f"scheduled_aurelius_abcdef12@2026101{i % 10}T100000-{i:04x}beef"
            rows = [
                {
                    "ts": ts,
                    "role": "user",
                    "content": f"[Autonomous scheduled task] run {i}",
                    "origin": "scheduled",
                }
            ]
        elif i % 6 == 5:
            tid = f"acp::aurelius::peer{i}"
            rows = [
                {
                    "ts": ts,
                    "role": "peer_inbound",
                    "content": f"peer question {i}",
                    "sender": "sally",
                }
            ]
        else:
            tid = f"chat-{i:04d}"
            rows = [
                {
                    "ts": ts,
                    "role": "user",
                    "content": f"human question {i} about invoices",
                    "channel": "dashboard",
                }
            ]
        rows.append({"ts": ts, "role": "assistant", "content": f"answer {i}"})
        path = sessions / f"{tid}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
        os.utime(path, (stamp, stamp))
        ids.append(tid)
    return ids


def test_pages_of_the_thread_list_join_back_into_the_whole_list(api_client):
    ids = _seed_threads(45)
    url = "/api/agents/aurelius/threads"
    whole = api_client.get(url).json()
    assert isinstance(whole, list) and len(whole) == 45

    seen: list[str] = []
    before = None
    pages = 0
    while True:
        body = api_client.get(url + "?limit=20" + (f"&before={before}" if before else "")).json()
        pages += 1
        seen += [t["id"] for t in body["threads"]]
        if not body["has_more"]:
            assert body["next_before"] is None
            break
        before = body["next_before"]
    assert pages == 3
    # Newest first, every thread once, the same set the whole list holds.
    assert seen == list(reversed(ids))
    assert seen == [t["id"] for t in whole]


def test_a_page_of_conversations_skips_runs_and_other_agents(api_client):
    ids = _seed_threads(30)
    body = api_client.get("/api/agents/aurelius/threads?limit=1&kind=conversation").json()
    assert len(body["threads"]) == 1
    newest_chat = next(t for t in reversed(ids) if t.startswith("chat-"))
    assert body["threads"][0]["id"] == newest_chat
    assert body["threads"][0]["origin"] == "interactive"
    assert body["has_more"] is True

    page = api_client.get("/api/agents/aurelius/threads?limit=50&kind=conversation").json()
    assert all(t["id"].startswith("chat-") for t in page["threads"])
    assert len(page["threads"]) == sum(1 for t in ids if t.startswith("chat-"))
    assert page["has_more"] is False


def test_a_page_hides_a_compaction_predecessor_it_reaches_after_its_successor(api_client):
    import os
    import time

    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "aurelius"
    sessions.mkdir(parents=True, exist_ok=True)
    now = time.time()
    (sessions / "old.jsonl").write_text(
        json.dumps({"ts": "t", "role": "user", "content": "long ago"}) + "\n"
    )
    os.utime(sessions / "old.jsonl", (now - 100, now - 100))
    (sessions / "new.jsonl").write_text(
        json.dumps(
            {
                "ts": "t",
                "role": "user",
                "content": "[Conversation compacted]\n\nPrior thread: old\n\nsummary",
            }
        )
        + "\n"
    )
    os.utime(sessions / "new.jsonl", (now, now))
    body = api_client.get("/api/agents/aurelius/threads?limit=10").json()
    assert [t["id"] for t in body["threads"]] == ["new"]


def test_bad_page_parameters_are_refused(api_client):
    _seed_threads(3)
    assert api_client.get("/api/agents/aurelius/threads?limit=0").status_code == 422
    assert api_client.get("/api/agents/aurelius/threads?before=abc").status_code == 422
    assert api_client.get("/api/agents/aurelius/threads?kind=everything").status_code == 422


def test_search_finds_the_word_with_a_snippet_newest_first(api_client):
    ids = _seed_threads(12)
    url = "/api/agents/aurelius/threads/search"
    body = api_client.get(url + "?q=INVOICES").json()
    chats = [t for t in reversed(ids) if t.startswith("chat-")]
    assert [t["id"] for t in body["threads"]] == chats
    first = body["threads"][0]
    assert first["match"]["role"] == "user"
    assert "invoices" in first["match"]["snippet"]
    assert body["has_more"] is False
    assert body["scanned"] == 12

    # A word nobody wrote, and a word with an accent (escaped in the file).
    assert api_client.get(url + "?q=zebra").json()["threads"] == []
    from olano.config.home import get_olano_paths

    sessions = get_olano_paths().sessions_dir / "aurelius"
    (sessions / "accent.jsonl").write_text(
        json.dumps({"ts": "t", "role": "assistant", "content": "La facturación está lista."}) + "\n"
    )
    hits = api_client.get(url + "?q=facturaci%C3%B3n").json()["threads"]
    assert [t["id"] for t in hits] == ["accent"]
    assert hits[0]["match"]["role"] == "assistant"


def test_search_pages_through_a_long_history(api_client, monkeypatch):
    ids = _seed_threads(10)
    monkeypatch.setattr(thread_pages, "SEARCH_MAX_SCAN", 4)
    url = "/api/agents/aurelius/threads/search?q=question"
    found: list[str] = []
    before = None
    requests = 0
    while True:
        body = api_client.get(url + (f"&before={before}" if before else "")).json()
        requests += 1
        found += [t["id"] for t in body["threads"]]
        assert body["scanned"] <= 4
        if not body["has_more"]:
            break
        before = body["next_before"]
    assert requests == 3
    # The human and the agent-to-agent threads ask a "question"; a scheduled run does not.
    assert found == [t for t in reversed(ids) if not t.startswith("scheduled_")]

    # A short query is refused; a limit caps a page.
    assert api_client.get("/api/agents/aurelius/threads/search?q=a").status_code == 422
    assert len(api_client.get(url + "&limit=2").json()["threads"]) == 2
