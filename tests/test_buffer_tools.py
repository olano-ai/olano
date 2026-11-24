"""Buffer scheduling on the GraphQL API.

Two things make this worth testing rather than trusting. First, GraphQL answers
**200 for a failed operation** — errors live in the body — so the naive "raise
for status, read the data" path reports a rejected post as scheduled and the
queue is quietly empty. Second, mutations return a union
(``PostActionSuccess | MutationError``) whose failure arm is just another object,
so it has to be split explicitly.

Everything here stubs ``httpx.post``. There is no live Buffer account in CI, and
the point is the request envelope and the error handling, not Buffer's uptime.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from olano.tools import buffer_tools
from olano.tools.buffer_tools import (
    _as_list,
    buffer_create_post,
    buffer_list_channels,
    buffer_list_scheduled,
)

ORG = {"data": {"account": {"organizations": [{"id": "org-1", "name": "Acme"}]}}}


def _response(payload: dict[str, Any]) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


class _Buffer:
    """Stub ``httpx.post`` that answers each GraphQL operation by name."""

    def __init__(self, **by_operation: dict[str, Any]):
        self.by_operation = by_operation
        self.requests: list[dict[str, Any]] = []

    def __call__(self, url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("json") or {}
        self.requests.append({"url": url, "headers": kwargs.get("headers", {}), **body})
        query = body.get("query", "")
        for name, payload in self.by_operation.items():
            if name in query:
                return _response(payload)
        return _response({"data": {}})

    @property
    def mutations(self) -> list[dict[str, Any]]:
        return [r for r in self.requests if "mutation" in r.get("query", "")]


@pytest.fixture(autouse=True)
def token(monkeypatch: pytest.MonkeyPatch):
    """A configured token, and no pinned org, unless a test says otherwise."""
    monkeypatch.setattr(
        buffer_tools,
        "get_credential",
        lambda name, default="": {"BUFFER_ACCESS_TOKEN": "tok"}.get(name, default),
    )


def _run(stub: _Buffer, fn: Any, **kwargs: Any) -> str:
    with patch("httpx.post", stub):
        return str(fn.func(**kwargs))


# ── Auth and organization resolution ───────────────────────────────────────


def test_missing_token_is_reported_as_missing_credentials(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(buffer_tools, "get_credential", lambda name, default="": default)
    assert buffer_list_channels.func().startswith("[missing-credentials]")


def test_bearer_token_is_sent_on_every_request():
    stub = _Buffer(organizations=ORG, ListChannels={"data": {"channels": []}})
    _run(stub, buffer_list_channels)
    assert stub.requests
    assert all(r["headers"]["Authorization"] == "Bearer tok" for r in stub.requests)


def test_pinned_organization_skips_the_account_lookup(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        buffer_tools,
        "get_credential",
        lambda name, default="": {
            "BUFFER_ACCESS_TOKEN": "tok",
            "BUFFER_ORGANIZATION_ID": "org-pinned",
        }.get(name, default),
    )
    stub = _Buffer(ListChannels={"data": {"channels": [{"id": "c1", "service": "x", "name": "N"}]}})
    _run(stub, buffer_list_channels)
    assert len(stub.requests) == 1
    assert stub.requests[0]["variables"]["input"]["organizationId"] == "org-pinned"


def test_account_with_no_organization_says_what_to_set():
    stub = _Buffer(organizations={"data": {"account": {"organizations": []}}})
    result = _run(stub, buffer_list_channels)
    assert "BUFFER_ORGANIZATION_ID" in result


# ── GraphQL error handling ─────────────────────────────────────────────────


def test_graphql_errors_on_a_200_are_surfaced_not_swallowed():
    """The failure this file exists for: a 200 whose body carries `errors`."""
    stub = _Buffer(
        organizations=ORG,
        ListChannels={"errors": [{"message": "Unknown field 'service'"}], "data": None},
    )
    result = _run(stub, buffer_list_channels)
    assert result.startswith("[tool-error]")
    assert "Unknown field 'service'" in result
    # Emphatically NOT the empty-list message, which would read as success.
    assert "No channels connected" not in result


def test_mutation_error_arm_is_reported_as_a_failure():
    stub = _Buffer(
        organizations=ORG,
        CreatePost={"data": {"createPost": {"message": "Text exceeds the limit for X"}}},
    )
    result = _run(stub, buffer_create_post, text="way too long", channel_ids=["c1"])
    assert result.startswith("[tool-error]")
    assert "Text exceeds the limit for X" in result


# ── Scheduling ─────────────────────────────────────────────────────────────


def _created(post_id: str = "p1") -> dict[str, Any]:
    return {"data": {"createPost": {"post": {"id": post_id, "text": "hi"}}}}


def test_no_scheduled_at_adds_to_the_queue():
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    result = _run(stub, buffer_create_post, text="hi", channel_ids=["c1"])
    payload = stub.mutations[0]["variables"]["input"]
    assert payload["mode"] == "addToQueue"
    assert "dueAt" not in payload
    assert result.startswith("Queued")


def test_scheduled_at_switches_to_a_custom_time():
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    _run(
        stub,
        buffer_create_post,
        text="hi",
        channel_ids=["c1"],
        scheduled_at="2026-09-01T14:30:00Z",
    )
    payload = stub.mutations[0]["variables"]["input"]
    assert payload["mode"] == "customScheduled"
    assert payload["dueAt"] == "2026-09-01T14:30:00Z"


def test_draft_does_not_schedule():
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    result = _run(stub, buffer_create_post, text="hi", channel_ids=["c1"], draft=True)
    assert stub.mutations[0]["variables"]["input"]["mode"] == "draft"
    assert result.startswith("Drafted")


def test_images_ride_along_as_assets():
    """The whole reason for the migration — the REST tools could not do this."""
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    _run(
        stub,
        buffer_create_post,
        text="hi",
        channel_ids=["c1"],
        image_urls=["https://cdn.example/a.png", "https://cdn.example/b.png"],
    )
    assets = stub.mutations[0]["variables"]["input"]["assets"]
    assert assets == [
        {"image": {"url": "https://cdn.example/a.png"}},
        {"image": {"url": "https://cdn.example/b.png"}},
    ]


def test_a_local_path_is_refused_with_the_reason_and_an_alternative():
    # Buffer fetches attachments itself, so a workspace path silently produces a
    # text-only post. Say so instead, and name the tools that CAN take a file.
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    result = _run(
        stub, buffer_create_post, text="hi", channel_ids=["c1"], image_urls=["renders/card.png"]
    )
    assert result.startswith("[tool-error]")
    assert "not a public URL" in result
    assert "instagram_publish_image" in result
    assert not stub.mutations


def test_no_channel_ids_points_at_the_lookup_tool():
    result = buffer_create_post.func(text="hi", channel_ids=[])
    assert "buffer_list_channels" in result


# ── Fan-out ────────────────────────────────────────────────────────────────


def test_one_call_creates_a_post_per_channel():
    stub = _Buffer(organizations=ORG, CreatePost=_created())
    result = _run(stub, buffer_create_post, text="hi", channel_ids=["c1", "c2", "c3"])
    assert len(stub.mutations) == 3
    assert [m["variables"]["input"]["channelId"] for m in stub.mutations] == ["c1", "c2", "c3"]
    assert "3 channel(s)" in result


def test_one_failing_channel_does_not_lose_the_others():
    """A rejected X post must not take LinkedIn and Facebook down with it."""
    calls: list[str] = []

    def _post(url: str, **kwargs: Any) -> MagicMock:
        body = kwargs.get("json") or {}
        if "organizations" in body.get("query", ""):
            return _response(ORG)
        channel = body["variables"]["input"]["channelId"]
        calls.append(channel)
        if channel == "c2":
            return _response({"data": {"createPost": {"message": "channel disconnected"}}})
        return _response(_created(f"post-{channel}"))

    with patch("httpx.post", _post):
        result = str(buffer_create_post.func(text="hi", channel_ids=["c1", "c2", "c3"]))

    assert calls == ["c1", "c2", "c3"]
    assert "2 channel(s)" in result
    assert "Failed on 1 channel(s)" in result
    assert "channel disconnected" in result


# ── Queue listing ──────────────────────────────────────────────────────────


def test_scheduled_posts_are_listed_with_their_due_time():
    stub = _Buffer(
        organizations=ORG,
        ScheduledPosts={
            "data": {
                "posts": {
                    "edges": [
                        {
                            "node": {
                                "id": "p1",
                                "text": "Launch\nday",
                                "dueAt": "2026-09-01T14:30:00Z",
                                "channelId": "c1",
                            }
                        }
                    ]
                }
            }
        },
    )
    result = _run(stub, buffer_list_scheduled)
    assert "[c1] Launch day @ 2026-09-01T14:30:00Z" in result


def test_limit_is_clamped_to_the_api_maximum():
    stub = _Buffer(organizations=ORG, ScheduledPosts={"data": {"posts": {"edges": []}}})
    _run(stub, buffer_list_scheduled, limit=500)
    assert stub.requests[-1]["variables"]["first"] == 50


def test_empty_queue_reads_as_empty_not_as_an_error():
    stub = _Buffer(organizations=ORG, ScheduledPosts={"data": {"posts": {"edges": []}}})
    assert _run(stub, buffer_list_scheduled) == "No scheduled posts."


# ── Input coercion ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (["a", "b"], ["a", "b"]),
        ("a,b", ["a", "b"]),
        ("a\nb", ["a", "b"]),
        (" a , , b ", ["a", "b"]),
        ("solo", ["solo"]),
        (None, []),
    ],
)
def test_id_lists_accept_every_shape_a_model_sends(value: Any, expected: list[str]):
    assert _as_list(value) == expected
