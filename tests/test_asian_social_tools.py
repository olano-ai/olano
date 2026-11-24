"""Tests for TikTok, Douyin and RedNote toolkits and integration agents."""

import json
from unittest.mock import Mock, patch

import pytest

from olano.subagents.registry import invalidate_cache, load_toolkit_agent
from olano.tools.registry import resolve_bundle, resolve_tools


@pytest.mark.parametrize("platform", ["tiktok", "douyin", "rednote"])
def test_social_toolkit_complete(platform):
    names = {tool.name for tool in resolve_bundle(platform)}
    assert names == {
        f"{platform}_{suffix}"
        for suffix in (
            "profile",
            "read_user",
            "search_posts",
            "list_posts",
            "read_post",
            "create_post",
            "update_post",
            "delete_post",
            "like_post",
            "unlike_post",
            "bookmark_post",
            "remove_bookmark",
            "read_comments",
            "reply_to_comment",
            "read_comment_replies",
            "react_to_comment",
            "remove_comment_reaction",
            "hide_comment",
            "delete_comment",
            "read_dms",
            "reply_to_dm",
            "read_dm_messages",
            "send_dm",
            "react_to_dm",
            "remove_dm_reaction",
            "mark_dm_read",
            "follow_user",
            "unfollow_user",
            "post_analytics",
            "account_analytics",
        )
    }


def test_read_post_uses_authenticated_adapter(monkeypatch):
    monkeypatch.setenv("TIKTOK_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("TIKTOK_API_BASE_URL", "https://partner.example/v1/")
    response = Mock(content=b"{}")
    response.raise_for_status.return_value = None
    response.json.return_value = {"id": "post-1"}
    with patch("olano.tools._short_video_client.requests.request", return_value=response) as req:
        result = resolve_tools(["tiktok_read_post"])[0].invoke({"post_id": "post-1"})
    assert json.loads(result) == {"id": "post-1"}
    req.assert_called_once()
    assert req.call_args.args[:2] == ("GET", "https://partner.example/v1/posts/post-1")
    assert req.call_args.kwargs["headers"]["Authorization"] == "Bearer secret"


def test_missing_credentials_is_actionable(monkeypatch):
    monkeypatch.delenv("DOUYIN_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("DOUYIN_API_BASE_URL", raising=False)
    result = resolve_tools(["douyin_read_dms"])[0].invoke({})
    assert "DOUYIN_ACCESS_TOKEN" in result and "DOUYIN_API_BASE_URL" in result


@pytest.mark.parametrize(
    ("tool_name", "arguments", "method", "path", "payload"),
    [
        (
            "rednote_react_to_comment",
            {"comment_id": "comment-1", "reaction": "❤️"},
            "PUT",
            "comments/comment-1/reaction",
            {"reaction": "❤️"},
        ),
        (
            "rednote_bookmark_post",
            {"post_id": "post-1"},
            "PUT",
            "posts/post-1/bookmark",
            None,
        ),
        (
            "rednote_react_to_dm",
            {"message_id": "message-1", "reaction": "🔥"},
            "PUT",
            "messages/message-1/reaction",
            {"reaction": "🔥"},
        ),
    ],
)
def test_reactions_and_bookmarks_route_to_adapter(
    monkeypatch, tool_name, arguments, method, path, payload
):
    monkeypatch.setenv("REDNOTE_ACCESS_TOKEN", "secret")
    monkeypatch.setenv("REDNOTE_API_BASE_URL", "https://partner.example/v1")
    response = Mock(content=b"{}")
    response.raise_for_status.return_value = None
    response.json.return_value = {"ok": True}
    with patch("olano.tools._short_video_client.requests.request", return_value=response) as req:
        resolve_tools([tool_name])[0].invoke(arguments)
    assert req.call_args.args[:2] == (method, f"https://partner.example/v1/{path}")
    assert req.call_args.kwargs["json"] == payload


def test_every_social_mutation_requires_hitl():
    from olano.security.hitl_policy import CATEGORY_TOOLS

    writes = CATEGORY_TOOLS["write_external"]
    for platform in ("tiktok", "douyin", "rednote"):
        assert f"{platform}_create_post" in writes
        assert f"{platform}_react_to_comment" in writes
        assert f"{platform}_bookmark_post" in writes
        assert f"{platform}_send_dm" in writes
        assert f"{platform}_read_post" not in writes


def test_integration_agents_and_xiaohongshu_alias():
    invalidate_cache()
    assert load_toolkit_agent("TikTokAgent").toolkit == "tiktok"
    assert load_toolkit_agent("DouyinAgent").toolkit == "douyin"
    assert load_toolkit_agent("XiaohongshuAgent").name == "RedNoteAgent"
