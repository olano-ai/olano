"""Tests for the Facebook + Instagram (Meta) toolkits.

All tests mock the Meta Business SDK / shared client — no live API calls.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from olano.tools import facebook_tools, instagram_tools

# ── Missing credentials ──────────────────────────────────────────────────────


def test_facebook_missing_credentials(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    out = facebook_tools.get_tool("facebook_list_pages").invoke({})
    assert out.startswith("[missing-credentials]")


def test_instagram_missing_credentials(monkeypatch):
    monkeypatch.delenv("META_ACCESS_TOKEN", raising=False)
    out = instagram_tools.get_tool("instagram_get_account").invoke({"ig_user_id": "123"})
    assert out.startswith("[missing-credentials]")


# ── Facebook tools ───────────────────────────────────────────────────────────


def test_facebook_list_pages_formats_rows():
    fake_api = MagicMock()
    pages = [
        {"id": "1", "name": "Acme", "category": "Business"},
        {"id": "2", "name": "Beta", "category": "Brand"},
    ]
    with (
        patch("olano.tools.facebook_tools.get_api", return_value=(fake_api, None)),
        patch("facebook_business.adobjects.user.User") as MockUser,
    ):
        MockUser.return_value.get_accounts.return_value = pages
        out = facebook_tools.get_tool("facebook_list_pages").invoke({})
    assert "Acme (id=1" in out
    assert "Beta (id=2" in out


def test_facebook_publish_post_uses_default_page(monkeypatch):
    monkeypatch.setenv("META_PAGE_ID", "999")
    fake_api = MagicMock()
    with (
        patch("olano.tools.facebook_tools.get_api", return_value=(fake_api, None)),
        patch("facebook_business.adobjects.page.Page") as MockPage,
    ):
        MockPage.return_value.create_feed.return_value = {"id": "999_555"}
        out = facebook_tools.get_tool("facebook_publish_post").invoke({"message": "hi"})
    MockPage.assert_called_once_with("999", api=fake_api)
    assert "999_555" in out


def test_facebook_publish_post_no_page_id(monkeypatch):
    monkeypatch.delenv("META_PAGE_ID", raising=False)
    fake_api = MagicMock()
    with patch("olano.tools.facebook_tools.get_api", return_value=(fake_api, None)):
        out = facebook_tools.get_tool("facebook_publish_post").invoke({"message": "hi"})
    assert "META_PAGE_ID is not set" in out


def test_facebook_hide_comment():
    fake_api = MagicMock()
    with (
        patch("olano.tools.facebook_tools.get_api", return_value=(fake_api, None)),
        patch("facebook_business.adobjects.comment.Comment") as MockComment,
    ):
        out = facebook_tools.get_tool("facebook_hide_comment").invoke({"comment_id": "c1"})
    MockComment.return_value.api_update.assert_called_once_with(params={"is_hidden": True})
    assert "hidden" in out


# ── Instagram tools ──────────────────────────────────────────────────────────


def test_instagram_publish_image_runs_container_flow(monkeypatch):
    monkeypatch.setenv("META_IG_USER_ID", "ig42")
    fake_api = MagicMock()
    user = MagicMock()
    user.create_media.return_value = {"id": "container1"}
    user.create_media_publish.return_value = {"id": "media1"}
    with (
        patch("olano.tools.instagram_tools.get_api", return_value=(fake_api, None)),
        patch("facebook_business.adobjects.iguser.IGUser", return_value=user),
    ):
        out = instagram_tools.get_tool("instagram_publish_image").invoke(
            {"image_url": "https://x/y.jpg", "caption": "hello"}
        )
    user.create_media.assert_called_once_with(
        params={"image_url": "https://x/y.jpg", "caption": "hello"}
    )
    user.create_media_publish.assert_called_once_with(params={"creation_id": "container1"})
    assert "media1" in out


def test_instagram_search_hashtag_two_step(monkeypatch):
    monkeypatch.setenv("META_IG_USER_ID", "ig42")
    search_resp = ({"data": [{"id": "h1"}]}, None)
    media_resp = ({"data": [{"id": "m1", "like_count": 5, "permalink": "http://p"}]}, None)
    with patch(
        "olano.tools.instagram_tools.graph_get", side_effect=[search_resp, media_resp]
    ) as mock_get:
        out = instagram_tools.get_tool("instagram_search_hashtag").invoke({"hashtag": "#cats"})
    # First call resolves the tag id, stripping the leading '#'.
    assert mock_get.call_args_list[0].args[0] == "ig_hashtag_search"
    assert mock_get.call_args_list[0].args[1]["q"] == "cats"
    assert "m1" in out


def test_instagram_search_hashtag_no_ig_user(monkeypatch):
    monkeypatch.delenv("META_IG_USER_ID", raising=False)
    out = instagram_tools.get_tool("instagram_search_hashtag").invoke({"hashtag": "cats"})
    assert "META_IG_USER_ID is not set" in out


# ── Registry + subagent wiring ───────────────────────────────────────────────


def test_meta_toolkits_registered():
    from olano.tools.registry import resolve_bundle

    fb = {t.name for t in resolve_bundle("facebook")}
    ig = {t.name for t in resolve_bundle("instagram")}
    meta = {t.name for t in resolve_bundle("meta")}
    assert "facebook_publish_post" in fb
    assert "instagram_publish_image" in ig
    assert meta == fb | ig


def test_meta_builtin_agents_resolve():
    from olano.subagents.registry import load_toolkit_agent, resolve_as_subagent

    fb = load_toolkit_agent("FacebookAgent")
    ig = load_toolkit_agent("InstagramAgent")
    assert fb.toolkit == "facebook"
    assert ig.toolkit == "instagram"
    # Toolkit slug also resolves to the hand-authored def.
    assert load_toolkit_agent("facebook").name == "FacebookAgent"
    sa = resolve_as_subagent("FacebookAgent")
    assert "facebook_publish_post" in sa.tools
