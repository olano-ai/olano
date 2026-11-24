"""Cortex jobs: background runs that outlive the page, never run twice, and
leave a record of what they did (``olano/cognition/jobs.py``).

The reported bug: **Create project** was a request the dashboard awaited, so
closing the panel left the card still offering *Create project*, a second tap
started a second promotion of the same idea, and nothing anywhere said what
the background run had done. These tests pin the three properties that fix it,
first on the registry and then through the real routes.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from olano.cognition import jobs as cj


@pytest.fixture(autouse=True)
def _fresh_registry():
    cj.REGISTRY.reset()
    yield
    cj.REGISTRY.reset()


# ── registry ────────────────────────────────────────────────────────────────


async def test_start_joins_the_running_job_instead_of_starting_another(tmp_path: Path) -> None:
    gate = asyncio.Event()
    calls = 0

    async def run(scope) -> None:
        nonlocal calls
        calls += 1
        await gate.wait()
        scope.complete(outcome="completed", summary="ok", result={"n": 1})

    first, started = cj.REGISTRY.start(
        agent="a", workspace_root=tmp_path, kind="promote", target="idea-1", run=run
    )
    again, started_again = cj.REGISTRY.start(
        agent="a", workspace_root=tmp_path, kind="promote", target="idea-1", run=run
    )
    assert started and not started_again
    assert again is first
    # A different target is a different job.
    other, started_other = cj.REGISTRY.start(
        agent="a", workspace_root=tmp_path, kind="promote", target="idea-2", run=run
    )
    assert started_other and other is not first

    gate.set()
    await asyncio.gather(first.task, other.task)
    assert calls == 2
    assert first.status == cj.DONE and first.result == {"n": 1}
    # Finished: the next press starts a fresh run.
    _, restarted = cj.REGISTRY.start(
        agent="a", workspace_root=tmp_path, kind="promote", target="idea-1", run=run
    )
    assert restarted


async def test_history_is_persisted_and_a_restart_reads_as_interrupted(tmp_path: Path) -> None:
    gate = asyncio.Event()

    async def run(scope) -> None:
        scope.note("step one")
        await gate.wait()

    job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="phase", run=run)
    await asyncio.sleep(0)
    job.flush()
    on_disk = cj.REGISTRY.history(tmp_path, "a")
    assert [r["id"] for r in on_disk] == [job.id]
    assert on_disk[0]["status"] == cj.RUNNING

    # The process "restarts": the record still says running, nothing is behind it.
    cj.REGISTRY.reset()
    detail = cj.REGISTRY.get(tmp_path, job.id)
    assert detail is not None
    assert detail["status"] == cj.INTERRUPTED
    assert any(e["text"] == "step one" for e in detail["log"])
    gate.set()
    await job.task


async def test_a_finished_job_keeps_its_log_on_disk(tmp_path: Path) -> None:
    async def run(scope) -> None:
        scope.note("did a thing", type="tool", tool="write_file")
        scope.complete(outcome="completed", summary="All done")

    job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="phase", run=run)
    await job.task
    cj.REGISTRY.reset()
    detail = cj.REGISTRY.get(tmp_path, job.id)
    assert detail["status"] == cj.DONE
    assert detail["summary"] == "All done"
    types = [e["type"] for e in detail["log"]]
    assert types[0] == "start" and types[-1] == "end"
    assert {"type": "tool", "tool": "write_file"}.items() <= next(
        e for e in detail["log"] if e["type"] == "tool"
    ).items()


async def test_a_nested_run_is_a_step_of_the_job_that_started_it(tmp_path: Path) -> None:
    async with cj.REGISTRY.track(
        agent="a", workspace_root=tmp_path, kind="generate", title="Think now"
    ) as outer:
        async with cj.REGISTRY.track(
            agent="a", workspace_root=tmp_path, kind="phase", target="ideas", title="Ideas"
        ) as inner:
            assert inner.nested and inner.job is outer.job
            inner.complete(outcome="completed", summary="3 ideas")
    assert len(cj.REGISTRY.history(tmp_path, "a")) == 1
    texts = [e["text"] for e in cj.REGISTRY.get(tmp_path, outer.job.id)["log"]]
    assert "Ideas: started" in texts
    assert "Ideas: completed. 3 ideas" in texts


async def test_stop_cancels_the_run_and_records_it(tmp_path: Path) -> None:
    async def run(scope) -> None:
        await asyncio.sleep(30)

    job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="optimize_skills", run=run)
    await asyncio.sleep(0)
    assert cj.REGISTRY.stop(job.id)
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert job.status == cj.STOPPED
    assert not cj.REGISTRY.stop(job.id)  # nothing left to stop
    assert cj.REGISTRY.find_live("a", "optimize_skills") is None


async def test_a_failed_outcome_is_an_error_not_a_success(tmp_path: Path) -> None:
    async def run(scope) -> None:
        scope.complete(outcome="timeout", error="Worker exceeded 600s timeout")

    job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="promote", run=run)
    await job.task
    assert job.status == cj.ERROR
    assert "600s" in job.error


async def test_worker_steps_are_captured_by_the_callback(tmp_path: Path) -> None:
    from uuid import uuid4

    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, LLMResult

    async with cj.REGISTRY.track(agent="a", workspace_root=tmp_path, kind="phase") as scope:
        (handler,) = cj.job_callbacks()
        rid = uuid4()
        handler.on_tool_start({"name": "write_file"}, "{'path': 'PLAN.md'}", run_id=rid)
        handler.on_tool_end("Wrote PLAN.md", run_id=rid)
        handler.on_llm_end(
            LLMResult(generations=[[ChatGeneration(message=AIMessage(content="Plan drafted."))]])
        )
    log = cj.REGISTRY.get(tmp_path, scope.job.id)["log"]
    tool = next(e for e in log if e["type"] == "tool")
    result = next(e for e in log if e["type"] == "tool_result")
    assert tool["tool"] == "write_file" and "PLAN.md" in tool["text"]
    assert result["tool"] == "write_file" and result["text"] == "Wrote PLAN.md"
    assert any(e["type"] == "text" and e["text"] == "Plan drafted." for e in log)
    # Outside a job a worker gets no callbacks at all.
    assert cj.job_callbacks() == []


async def test_the_log_is_bounded(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cj, "MAX_LOG_ENTRIES", 5)
    async with cj.REGISTRY.track(agent="a", workspace_root=tmp_path, kind="phase") as scope:
        for i in range(20):
            scope.note(f"line {i}")
        assert len(scope.job.log) == 5
        assert scope.job.log_dropped > 0
        assert scope.job.log[-1]["text"] == "line 19"


async def test_history_is_pruned(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cj, "MAX_HISTORY", 3)
    for _ in range(6):
        async with cj.REGISTRY.track(agent="a", workspace_root=tmp_path, kind="phase"):
            pass
        time.sleep(0.01)  # distinct mtimes
    assert len(cj.REGISTRY.history(tmp_path, "a", limit=100)) <= 4


# ── routes ──────────────────────────────────────────────────────────────────


@pytest.fixture
def api(tmp_path, monkeypatch, base_config):
    """A real API over one agent whose promotion we control."""
    from olano.cognition import cortex_items as ci
    from olano.cognition import ideation
    from olano.cognition.worker import WorkerOutcome
    from olano.runtime.api import create_api

    ws = Path(base_config.workspace)
    item = ci.add_item(ws, kind="idea", title="Catalogue readiness gate", summary="s")
    calls: list[str] = []

    async def fake_run_promote(config, workspace_root, it, *, target=None, source="manual", **kw):
        calls.append(it["id"])
        from olano.cognition import jobs

        jobs.note("writing PLAN.md", type="tool", tool="write_file")
        await asyncio.sleep(0.4)
        project = ci.add_item(workspace_root, kind="project", title=it["title"])
        ci.set_status(workspace_root, it["id"], "promoted")
        return WorkerOutcome(status="completed", summary="Planned 3 phases."), project

    monkeypatch.setattr(ideation, "run_promote", fake_run_promote)
    runtime = MagicMock()
    runtime._runners = {base_config.name: MagicMock(config=base_config)}
    with TestClient(create_api(runtime)) as client:
        yield client, base_config.name, item, calls


def _wait_done(client, agent: str, job_id: str, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/cortex/jobs/{job_id}", params={"agent": agent}).json()["job"]
        if job["status"] != "running":
            return job
        time.sleep(0.1)
    raise AssertionError("job did not finish")


def test_create_project_runs_in_the_background_and_is_never_started_twice(api) -> None:
    client, agent, item, calls = api
    url = f"/api/cortex/items/{item['id']}/promote"

    first = client.post(url, json={"agent": agent, "background": True}).json()
    assert first["ok"] and first["already_running"] is False
    job = first["job"]
    assert job["status"] == "running" and job["kind"] == "promote"

    # A second tap (or a second tab) joins the same run.
    second = client.post(url, json={"agent": agent, "background": True}).json()
    assert second["already_running"] is True and second["job"]["id"] == job["id"]

    # The card knows it is being promoted, and by which run.
    rows = client.get("/api/cortex/items", params={"kind": "idea", "all_agents": "true"}).json()[
        "items"
    ]
    card = next(r for r in rows if r["id"] == item["id"])
    assert card["job"]["id"] == job["id"] and card["job"]["status"] == "running"

    # So does the item page: the phone acts on the detail, not the list, and
    # without the stamp its "Make it a project" button came back the moment
    # the request answered, as if nothing had happened.
    detail = client.get(f"/api/cortex/items/{item['id']}", params={"agent": agent}).json()
    assert detail["item"]["job"]["id"] == job["id"]
    assert detail["item"]["job"]["status"] == "running"
    assert detail["item"]["job"]["detail"]["to"] == "project"

    # It is listed as running.
    active = client.get("/api/cortex/jobs", params={"all_agents": "true", "active": "true"}).json()
    assert [j["id"] for j in active["jobs"]] == [job["id"]]

    done = _wait_done(client, agent, job["id"])
    assert done["status"] == "done"
    assert done["result"]["project"]["kind"] == "project"
    assert any(e.get("tool") == "write_file" for e in done["log"])
    assert calls == [item["id"]]

    # Promoted now: a late tap is refused instead of creating a second project.
    again = client.post(url, json={"agent": agent, "background": True})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "already_promoted"

    history = client.get("/api/cortex/jobs", params={"agent": agent}).json()["jobs"]
    assert history[0]["id"] == job["id"] and history[0]["status"] == "done"


def test_the_blocking_contract_still_answers_with_the_result(api) -> None:
    client, agent, item, _calls = api
    res = client.post(f"/api/cortex/items/{item['id']}/promote", json={"agent": agent})
    assert res.status_code == 200
    body = res.json()
    assert body["project"]["kind"] == "project" and body["summary"] == "Planned 3 phases."


def test_a_running_promotion_can_be_stopped(api) -> None:
    client, agent, item, _calls = api
    job = client.post(
        f"/api/cortex/items/{item['id']}/promote", json={"agent": agent, "background": True}
    ).json()["job"]
    stopped = client.post(f"/api/cortex/jobs/{job['id']}/stop", json={})
    assert stopped.status_code == 200 and stopped.json()["ok"] is True
    assert _wait_done(client, agent, job["id"])["status"] == "stopped"
    assert client.post(f"/api/cortex/jobs/{job['id']}/stop", json={}).status_code == 404


def test_an_unknown_job_is_a_404(api) -> None:
    client, agent, _item, _calls = api
    assert client.get("/api/cortex/jobs/nope", params={"agent": agent}).status_code == 404


async def test_a_periodic_flush_never_overwrites_the_final_record(
    tmp_path: Path, monkeypatch
) -> None:
    """A periodic flush snapshotted mid-run must not land after the final one.

    Every note may schedule a background write; the job's close writes the
    finished record synchronously. Without one lock across snapshot + write, a
    background write that took its snapshot before the end could finish after
    it and put a "running" record with no end step back on disk (seen on CI).
    """
    import threading

    monkeypatch.setattr(cj, "PERSIST_INTERVAL_S", 0.0)
    real_write = cj._atomic_write_json

    def slow_off_loop_write(path, doc):
        # A slow disk under the background writer only: the window in which
        # its snapshot is already stale when the run's final write happens.
        if threading.current_thread() is not threading.main_thread():
            time.sleep(0.05)
        real_write(path, doc)

    monkeypatch.setattr(cj, "_atomic_write_json", slow_off_loop_write)
    for _ in range(5):

        async def run(scope) -> None:
            for i in range(50):
                scope.note(f"step {i}")
            scope.complete(outcome="completed", summary="ok")

        job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="phase", run=run)
        await job.task
        # Let any background write still in the executor finish.
        await asyncio.get_running_loop().run_in_executor(None, lambda: None)
        await asyncio.sleep(0.3)
        cj.REGISTRY.reset()
        detail = cj.REGISTRY.get(tmp_path, job.id)
        assert detail["status"] == cj.DONE
        assert detail["log"][-1]["type"] == "end"


async def test_a_stop_before_the_first_step_still_closes_the_job(tmp_path: Path) -> None:
    """Start, then Stop in the same tick: the job's task is cancelled before its
    body ever runs, so nothing inside it can close the job. It must still end
    as stopped, on disk too, not sit "running" until a restart calls it
    interrupted."""
    ran = False

    async def run(scope) -> None:
        nonlocal ran
        ran = True

    job, _ = cj.REGISTRY.start(agent="a", workspace_root=tmp_path, kind="promote", run=run)
    assert cj.REGISTRY.stop(job.id)
    with pytest.raises(asyncio.CancelledError):
        await job.task
    assert not ran
    assert job.status == cj.STOPPED
    assert cj.REGISTRY.find_live("a", "promote") is None
    cj.REGISTRY.reset()
    assert cj.REGISTRY.get(tmp_path, job.id)["status"] == cj.STOPPED


async def test_a_nested_step_that_raises_is_recorded_in_the_parent(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        async with cj.REGISTRY.track(agent="a", workspace_root=tmp_path, kind="generate") as outer:
            async with cj.REGISTRY.track(
                agent="a", workspace_root=tmp_path, kind="phase", target="ideas", title="Ideas"
            ):
                raise RuntimeError("model unreachable")
    log = cj.REGISTRY.get(tmp_path, outer.job.id)["log"]
    assert any(e["type"] == "step_error" and "model unreachable" in e["text"] for e in log)


async def test_recent_jobs_are_capped_by_count(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(cj, "RECENT_MAX", 3)
    ids = []
    for _ in range(6):
        async with cj.REGISTRY.track(agent="a", workspace_root=tmp_path, kind="phase") as scope:
            ids.append(scope.job.id)
        await asyncio.sleep(0.005)
    cj.REGISTRY.find_live("a", "phase")  # any accessor sweeps
    kept = [i for i in ids if cj.REGISTRY.get_memory(i) is not None]
    assert kept == ids[-3:]
    # The disk still has every one of them.
    assert len(cj.REGISTRY.history(tmp_path, "a", limit=100)) == 6


def test_a_blocking_request_reports_a_stopped_run(api) -> None:
    """A caller on the old blocking contract waits on the job; stopping the job
    from elsewhere ends that wait with an error, not a hang and not a 200."""
    from concurrent.futures import ThreadPoolExecutor

    client, agent, item, _calls = api
    url = f"/api/cortex/items/{item['id']}/promote"
    with ThreadPoolExecutor(1) as pool:
        waiting = pool.submit(client.post, url, json={"agent": agent})
        deadline = time.time() + 5
        job_id = ""
        while time.time() < deadline and not job_id:
            active = client.get(
                "/api/cortex/jobs", params={"agent": agent, "active": "true"}
            ).json()
            if active["jobs"]:
                job_id = active["jobs"][0]["id"]
            else:
                time.sleep(0.05)
        assert job_id, "the blocking request never started its job"
        assert client.post(f"/api/cortex/jobs/{job_id}/stop", json={}).status_code == 200
        res = waiting.result(timeout=10)
    assert res.status_code == 500
    assert "Stopped" in res.json()["detail"]
