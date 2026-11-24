"""Unit tests for the generic monitor (watchdog state) toolkit."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from olano.security.request_context import push_request_context


@pytest.fixture
def ctx(tmp_path: Path):
    """Bind a request context so monitor tools resolve their state under tmp_path."""
    with push_request_context(source="test", metadata={"workspace_root": str(tmp_path)}):
        yield tmp_path


def _call(tool, **kwargs):
    # Call the underlying function directly (bypasses LangChain arg coercion).
    return tool.func(**kwargs)


def test_seen_check_then_add_dedupes_across_runs(ctx):
    from olano.tools import monitor_tools as m

    ns = "sicop_servicios"
    ids = ["2024LN-001", "2024LN-002", "2024LN-003"]

    # Run 1: everything is new.
    out = _call(m.monitor_seen_check, namespace=ns, ids=ids)
    assert "3 new" in out
    for i in ids:
        assert i in out
    _call(m.monitor_seen_add, namespace=ns, ids=ids)

    # Run 2: one repeat + one new → only the new one surfaces.
    out2 = _call(m.monitor_seen_check, namespace=ns, ids=["2024LN-002", "2024LN-009"])
    assert "1 new" in out2
    assert "2024LN-009" in out2
    assert "1 already seen" in out2


def test_seen_add_is_idempotent_and_keeps_first_seen(ctx):
    from olano.tools import monitor_tools as m

    ns = "jobs"
    _call(m.monitor_seen_add, namespace=ns, ids=["A"], note="first")
    state_file = ctx / "memories" / "monitor_state" / "jobs.json"
    first_ts = json.loads(state_file.read_text())["seen"]["A"]["first_seen"]

    # Re-adding must not change first_seen or duplicate the entry.
    res = _call(m.monitor_seen_add, namespace=ns, ids=["A", "B"])
    assert "1 new" in res  # only B counts as newly added
    data = json.loads(state_file.read_text())
    assert data["seen"]["A"]["first_seen"] == first_ts
    assert set(data["seen"]) == {"A", "B"}


def test_ids_are_normalized(ctx):
    from olano.tools import monitor_tools as m

    ns = "norm"
    # Whitespace, blanks and duplicates collapse to two distinct ids.
    _call(m.monitor_seen_add, namespace=ns, ids=[" X ", "X", "", "  ", "Y"])
    data = json.loads((ctx / "memories" / "monitor_state" / "norm.json").read_text())
    assert set(data["seen"]) == {"X", "Y"}


def test_namespaces_are_isolated(ctx):
    from olano.tools import monitor_tools as m

    _call(m.monitor_seen_add, namespace="alpha", ids=["1"])
    _call(m.monitor_seen_add, namespace="beta", ids=["2"])
    out = _call(m.monitor_seen_check, namespace="alpha", ids=["2"])
    # id "2" belongs to beta, so it is NEW from alpha's perspective.
    assert "1 new" in out


def test_run_log_and_history(ctx):
    from olano.tools import monitor_tools as m

    ns = "runs"
    _call(m.monitor_run_log, namespace=ns, found=5, new=2, note="delivered")
    _call(m.monitor_run_log, namespace=ns, found=3, new=0, note="no-op")
    hist = _call(m.monitor_run_history, namespace=ns, limit=10)
    assert "found=5" in hist and "found=3" in hist
    # Newest first.
    assert hist.index("no-op") < hist.index("delivered")


def test_feedback_records_and_filters(ctx):
    from olano.tools import monitor_tools as m

    ns = "fb"
    _call(
        m.monitor_feedback_add,
        namespace=ns,
        item_id="2024LN-001",
        label="Irrelevant",
        reason="telecomunicaciones",
    )
    _call(m.monitor_feedback_add, namespace=ns, item_id="2024LN-002", label="relevant")

    only_irrelevant = _call(m.monitor_feedback_list, namespace=ns, label="irrelevant")
    assert "2024LN-001" in only_irrelevant
    assert "2024LN-002" not in only_irrelevant
    # Label is normalized to lowercase on store.
    assert "[irrelevant]" in only_irrelevant


def test_state_summary_reports_counts(ctx):
    from olano.tools import monitor_tools as m

    ns = "sum"
    _call(m.monitor_seen_add, namespace=ns, ids=["a", "b", "c"])
    _call(m.monitor_run_log, namespace=ns, found=3, new=3, note="delivered")
    _call(m.monitor_feedback_add, namespace=ns, item_id="a", label="irrelevant")
    summary = _call(m.monitor_state_summary, namespace=ns)
    assert "tracked ids: 3" in summary
    assert "runs logged: 1" in summary
    assert "1 marked irrelevant" in summary


def test_corrupt_state_file_recovers(ctx):
    from olano.tools import monitor_tools as m

    ns = "broken"
    path = ctx / "memories" / "monitor_state" / "broken.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ this is not valid json")

    # Should not raise; starts fresh and preserves the bad copy for forensics.
    out = _call(m.monitor_seen_add, namespace=ns, ids=["ok"])
    assert "1 new" in out
    assert path.with_suffix(".json.corrupt").exists()


def test_empty_batch_reports_no_new(ctx):
    from olano.tools import monitor_tools as m

    out = _call(m.monitor_seen_check, namespace="empty", ids=[])
    assert "0 new" in out
    assert "No new ids" in out


def test_concurrent_saves_do_not_crash(ctx, caplog):
    """Two writers hitting the same namespace must not race on a shared temp
    file and blow up with FileNotFoundError (the ``sicop_markline.json`` bug).

    Each write uses a *unique* temp file, so the race can't fire at all — we
    assert both that nothing escapes AND that ``_persist`` never had to fall
    back to its retry/degrade path (which would flag a regression to a shared
    temp name even though the safety net would still hide the crash).
    """
    import logging
    import threading

    from olano.tools import monitor_tools as m

    ns = "race"
    root = str(ctx)
    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker(n: int) -> None:
        # contextvars don't propagate into new threads — bind the workspace here.
        with push_request_context(source="test", metadata={"workspace_root": root}):
            barrier.wait()  # release all writers together to maximize overlap
            try:
                for j in range(5):
                    _call(m.monitor_seen_add, namespace=ns, ids=[f"t{n}-{j}"])
            except BaseException as exc:  # recorded so the assertion can report it
                errors.append(exc)

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
    with caplog.at_level(logging.WARNING, logger=m.logger.name):
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    assert not errors, f"concurrent saves raised: {errors!r}"
    # The race never fired — no save fell back to the retry/degrade path.
    assert "save failed" not in caplog.text
    # State file survived and is still valid JSON (no partial/corrupt write)…
    path = ctx / "memories" / "monitor_state" / "race.json"
    data = json.loads(path.read_text())
    assert isinstance(data.get("seen"), dict) and data["seen"]
    # …and no leftover temp file was left behind.
    assert not list(path.parent.glob("*.tmp"))


def test_persist_failure_degrades_gracefully(ctx, monkeypatch):
    """A persistent write failure returns an actionable message instead of
    raising — a raised tool error would abort the whole agent turn (AgentError).
    """
    from olano.tools import monitor_tools as m

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise OSError("disk gone")

    monkeypatch.setattr(m, "atomic_write_text", boom)
    monkeypatch.setattr(m.time, "sleep", lambda *_a, **_k: None)  # skip backoff wait

    out = _call(m.monitor_seen_add, namespace="degraded", ids=["x"])
    assert out.startswith("⚠️")
    assert "Could not persist" in out
    # It retried before giving up rather than failing on the first error.
    assert calls["n"] == m._SAVE_RETRIES


def test_all_tools_resolve_via_registry():
    from olano.tools.registry import resolve_tools

    names = [
        "monitor_seen_check",
        "monitor_seen_add",
        "monitor_seen_list",
        "monitor_run_log",
        "monitor_run_history",
        "monitor_feedback_add",
        "monitor_feedback_list",
        "monitor_state_summary",
    ]
    resolved = resolve_tools(names)
    assert {t.name for t in resolved} == set(names)
    # The bundle name also resolves to the full toolkit.
    from olano.tools.registry import resolve_bundle

    assert len(resolve_bundle("monitor")) == len(names)
