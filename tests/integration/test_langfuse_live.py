"""A real traced turn, exported to a real Langfuse project and read back.

Opt-in: runs only with ``OLANO_LANGFUSE_LIVE=1`` and Langfuse keys in the
environment the suite started in (``LANGFUSE_PUBLIC_KEY``,
``LANGFUSE_SECRET_KEY``, ``LANGFUSE_BASE_URL``; ``tests/conftest.py``
snapshots them before removing them for every other test). Skips otherwise.

What it proves that the in-process tests cannot: the ingestion endpoint and
headers Olano builds are accepted, the trace arrives with its name, session
and tags, the tool span nests under the turn, ``metadata`` capture stores no
text on the server, and the delete the dashboard will use works. Every trace
it writes is deleted at the end; the content is synthetic.

    OLANO_LANGFUSE_LIVE=1 uv run pytest tests/integration/test_langfuse_live.py -q
"""

from __future__ import annotations

import datetime as dt
import json
import os
import time
import uuid

import httpx
import pytest
import yaml

from olano.config.home import get_olano_paths
from olano.observability import credentials, tracing
from tests.conftest import LANGFUSE_ENV_SNAPSHOT

pytestmark = pytest.mark.skipif(
    os.environ.get("OLANO_LANGFUSE_LIVE") != "1"
    or not (
        LANGFUSE_ENV_SNAPSHOT.get("LANGFUSE_PUBLIC_KEY")
        and LANGFUSE_ENV_SNAPSHOT.get("LANGFUSE_SECRET_KEY")
    ),
    reason="live Langfuse test: set OLANO_LANGFUSE_LIVE=1 with LANGFUSE_* keys",
)

MARKER = "olano-live-test"


@pytest.fixture
def live_env(monkeypatch, lionclaw_home):
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", "LANGFUSE_HOST"):
        if key in LANGFUSE_ENV_SNAPSHOT:
            monkeypatch.setenv(key, LANGFUSE_ENV_SNAPSHOT[key])
    credentials._reset_for_tests()
    tracing._reset_for_tests()
    yield {
        "pk": LANGFUSE_ENV_SNAPSHOT["LANGFUSE_PUBLIC_KEY"],
        "sk": LANGFUSE_ENV_SNAPSHOT["LANGFUSE_SECRET_KEY"],
        "base": (
            LANGFUSE_ENV_SNAPSHOT.get("LANGFUSE_BASE_URL")
            or LANGFUSE_ENV_SNAPSHOT.get("LANGFUSE_HOST")
            or "https://cloud.langfuse.com"
        ).rstrip("/"),
    }
    tracing.shutdown_all()
    tracing._reset_for_tests()


def _enable(capture: str) -> None:
    path = get_olano_paths().config_path
    raw = (yaml.safe_load(path.read_text()) if path.exists() else {}) or {}
    raw["observability"] = {
        "langfuse": {"enabled": True, "capture": capture, "environment": "olano-test"}
    }
    path.write_text(yaml.safe_dump(raw))


def _observations(env, trace_id: str) -> list[dict]:
    """Poll the v2 observations API (the only read path new orgs get without
    hard rate limits) until the trace's spans are there."""
    frm = (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=15)).isoformat()
    to = (dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)).isoformat()
    for _ in range(18):
        time.sleep(10)
        r = httpx.get(
            f"{env['base']}/api/public/v2/observations",
            params={
                "traceId": trace_id,
                "fromStartTime": frm,
                "toStartTime": to,
                "fields": "core,basic,io",
            },
            auth=(env["pk"], env["sk"]),
            timeout=30,
        )
        if r.status_code == 200:
            data = r.json().get("data") or []
            if len(data) >= 3:
                return data
    pytest.fail(f"trace {trace_id} did not arrive (last HTTP {r.status_code})")


def _delete(env, trace_id: str) -> None:
    httpx.delete(
        f"{env['base']}/api/public/traces/{trace_id}", auth=(env["pk"], env["sk"]), timeout=30
    )


async def _turn(base_config, monkeypatch, message: str, reply: str, thread_id: str):
    monkeypatch.setenv(
        "LIONCLAW_FAKE_MODEL_SCRIPT",
        json.dumps([{"tool": "ls", "args": {"path": "."}}, {"text": reply}]),
    )
    base_config.model = "fake:instant"
    from olano.core.builder import build_agent_async
    from olano.core.runner import AgentRunner

    runner = AgentRunner(await build_agent_async(base_config), base_config)
    async for _ in runner.astream_events(message, thread_id=thread_id, turn_id="live-1"):
        pass
    rows = [json.loads(line) for line in runner._session_path(thread_id).read_text().splitlines()]
    trace_id = next(r["trace_id"] for r in rows if r.get("role") == "assistant")
    for client in list(tracing._clients.values()):
        client.langfuse.flush()
    return trace_id


async def test_live_metadata_turn(base_config, monkeypatch, live_env):
    _enable("metadata")
    secret_words = f"{MARKER}-private-{uuid.uuid4().hex[:8]}"
    thread = f"{MARKER}-{uuid.uuid4().hex[:8]}"
    trace_id = await _turn(
        base_config, monkeypatch, f"question {secret_words}", f"answer {secret_words}", thread
    )
    try:
        obs = _observations(live_env, trace_id)
        by_name = {o["name"]: o for o in obs}
        assert "turn" in by_name and "ls" in by_name, list(by_name)
        root = by_name["turn"]
        # The tool span sits under the graph's own chain spans; its ancestry
        # must reach the turn's root.
        parents = {o["id"]: o.get("parentObservationId") for o in obs}
        node, hops = by_name["ls"]["id"], 0
        while node and node != root["id"] and hops < 10:
            node, hops = parents.get(node), hops + 1
        assert node == root["id"]
        assert root["sessionId"].endswith(thread)
        assert root.get("environment") in ("olano-test", None)
        assert secret_words not in json.dumps(obs)
        assert any(o["type"] == "GENERATION" for o in obs)
    finally:
        _delete(live_env, trace_id)


async def test_live_full_turn_masks_secrets(base_config, monkeypatch, live_env):
    _enable("full")
    # A secret no shape rule knows (a random value in a *_PASSWORD env var):
    # only the value mask can catch it, and the runner's own output scrubber
    # cannot, so this proves the mask on the way out.
    secret = uuid.uuid4().hex
    monkeypatch.setenv("OLANO_LIVE_TEST_PASSWORD", secret)
    thread = f"{MARKER}-{uuid.uuid4().hex[:8]}"
    trace_id = await _turn(
        base_config, monkeypatch, f"say the word {secret}", f"the word is {secret}", thread
    )
    try:
        blob = json.dumps(_observations(live_env, trace_id))
        assert "say the word" in blob
        assert secret not in blob
        assert "[secret]" in blob
    finally:
        _delete(live_env, trace_id)


def _feedback_score(env, trace_id: str, want: tuple[float, int]) -> tuple[float, int]:
    """Poll the v2 metrics API (score reads are legacy-only, 410 on new
    orgs) for the trace's ``user_feedback`` (sum, count) until it reads
    ``want``; return the last reading."""
    now = dt.datetime.now(dt.UTC)
    query = {
        "view": "scores-numeric",
        "metrics": [
            {"measure": "value", "aggregation": "sum"},
            {"measure": "count", "aggregation": "count"},
        ],
        "dimensions": [{"field": "name"}],
        "filters": [{"column": "traceId", "operator": "=", "value": trace_id, "type": "string"}],
        "fromTimestamp": (now - dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
        "toTimestamp": (now + dt.timedelta(hours=1)).isoformat().replace("+00:00", "Z"),
    }
    last = (0.0, 0)
    for _ in range(18):
        time.sleep(10)
        r = httpx.get(
            f"{env['base']}/api/public/v2/metrics",
            params={"query": json.dumps(query)},
            auth=(env["pk"], env["sk"]),
            timeout=30,
        )
        if r.status_code != 200:
            continue
        rows = [d for d in r.json().get("data", []) if d.get("name") == "user_feedback"]
        last = (
            (float(rows[0].get("sum_value") or 0), int(rows[0].get("count_count") or 0))
            if rows
            else (0.0, 0)
        )
        if last == want:
            return last
    return last


async def test_live_vote_becomes_one_score(base_config, monkeypatch, live_env):
    """A dashboard vote reaches the reply's trace as a `user_feedback` score,
    and a changed vote replaces it rather than adding a second one."""
    from olano.core.feedback import turn_trace_id
    from olano.observability.feedback import send_feedback

    _enable("metadata")
    thread = f"{MARKER}-{uuid.uuid4().hex[:8]}"
    trace_id = await _turn(base_config, monkeypatch, "rate me", "a reply to rate", thread)
    try:
        assert turn_trace_id(base_config.name, thread, "live-1") == trace_id
        assert send_feedback(base_config, trace_id, user="live-tester", value=-1, comment="x")
        for client in list(tracing._clients.values()):
            client.langfuse.flush()
        assert _feedback_score(live_env, trace_id, (-1.0, 1)) == (-1.0, 1)
        assert send_feedback(base_config, trace_id, user="live-tester", value=1)
        for client in list(tracing._clients.values()):
            client.langfuse.flush()
        assert _feedback_score(live_env, trace_id, (1.0, 1)) == (1.0, 1)
    finally:
        _delete(live_env, trace_id)
