"""Instagram tools (Meta Graph API via the Meta Business SDK).

Agent-facing surface for Instagram Business/Creator accounts: account info,
content publishing (the container -> publish flow), media insights, comment
moderation, and hashtag search. Implementation sits on the shared
:mod:`olano.tools._meta_client` layer (SDK + graph escape hatch).

Auth env vars (vault-backed): ``META_ACCESS_TOKEN`` (required),
``META_IG_USER_ID`` (optional default IG user id).
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._meta_client import (
    default_ig_user_id,
    get_api,
    graph_get,
    meta_error,
)
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_NO_IG_MSG = (
    "[tool-error] No Instagram user id provided and META_IG_USER_ID is not set. "
    "Pass ig_user_id explicitly or set META_IG_USER_ID in the vault."
)


def _publish_container(api, ig_user_id: str, container_params: dict) -> str:
    """Run the IG two-step publish flow: create container then publish it."""
    from facebook_business.adobjects.iguser import IGUser

    user = IGUser(ig_user_id, api=api)
    container = user.create_media(params=container_params)
    creation_id = container.get("id")
    if not creation_id:
        return "[tool-error] Instagram publish: container creation returned no id."
    published = user.create_media_publish(params={"creation_id": creation_id})
    return f"Instagram media published (id={published.get('id', 'unknown')})."


@tool
def instagram_get_account(
    ig_user_id: Annotated[str, "IG user id (defaults to META_IG_USER_ID)"] = "",
) -> str:
    """Get the connected Instagram Business/Creator account profile."""
    api, err = get_api()
    if err:
        return err
    uid = ig_user_id.strip() or default_ig_user_id()
    if not uid:
        return _NO_IG_MSG
    try:
        from facebook_business.adobjects.iguser import IGUser

        u = IGUser(uid, api=api).api_get(
            fields=["username", "name", "followers_count", "media_count", "biography"]
        )
        return (
            f"Instagram account {uid}:\n"
            f"  Username: @{u.get('username', '?')}\n"
            f"  Name: {u.get('name', '?')}\n"
            f"  Followers: {u.get('followers_count', '?')}\n"
            f"  Media count: {u.get('media_count', '?')}\n"
            f"  Bio: {u.get('biography', '')}"
        )
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_publish_image(
    image_url: Annotated[str, "Public URL of the image to publish"],
    caption: Annotated[str, "Caption text"] = "",
    ig_user_id: Annotated[str, "IG user id (defaults to META_IG_USER_ID)"] = "",
) -> str:
    """Publish an image to Instagram (creates a media container, then publishes)."""
    api, err = get_api()
    if err:
        return err
    uid = ig_user_id.strip() or default_ig_user_id()
    if not uid:
        return _NO_IG_MSG
    try:
        return _publish_container(api, uid, {"image_url": image_url.strip(), "caption": caption})
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_publish_reel(
    video_url: Annotated[str, "Public URL of the video to publish as a Reel"],
    caption: Annotated[str, "Caption text"] = "",
    ig_user_id: Annotated[str, "IG user id (defaults to META_IG_USER_ID)"] = "",
) -> str:
    """Publish a Reel to Instagram (container -> publish).

    Note: video containers process asynchronously; if publishing fails because
    the container is not ready, retry shortly.
    """
    api, err = get_api()
    if err:
        return err
    uid = ig_user_id.strip() or default_ig_user_id()
    if not uid:
        return _NO_IG_MSG
    try:
        return _publish_container(
            api,
            uid,
            {"media_type": "REELS", "video_url": video_url.strip(), "caption": caption},
        )
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_get_media_insights(
    media_id: Annotated[str, "Instagram media id"],
    metrics: Annotated[str, "Comma-separated metrics"] = "impressions,reach,engagement",
) -> str:
    """Get insight metrics for an Instagram media item (reach, impressions, engagement)."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.igmedia import IGMedia

        results = IGMedia(media_id, api=api).get_insights(params={"metric": metrics})
        rows: list[str] = []
        for m in results:
            values = m.get("values") or []
            val = values[0].get("value") if values else "?"
            rows.append(f"  {m.get('name', '?')}: {val}")
        if not rows:
            return f"No insight data returned for media {media_id}."
        return f"Instagram media {media_id} insights:\n" + "\n".join(rows)
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_get_comments(
    media_id: Annotated[str, "Instagram media id"],
    limit: Annotated[int, "Max comments to return"] = 25,
) -> str:
    """List comments on an Instagram media item (id, author, text, time)."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.igmedia import IGMedia

        comments = IGMedia(media_id, api=api).get_comments(
            fields=["id", "text", "username", "timestamp"],
            params={"limit": limit},
        )
        rows = [
            f"  [{c.get('id', '?')}] @{c.get('username', '?')}: {c.get('text', '')} "
            f"({c.get('timestamp', '')})"
            for c in comments
        ]
        if not rows:
            return f"No comments on media {media_id}."
        return f"Comments on Instagram media {media_id}:\n" + "\n".join(rows)
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_reply_to_comment(
    comment_id: Annotated[str, "Instagram comment id to reply to"],
    message: Annotated[str, "Reply text"],
) -> str:
    """Reply to a comment on an Instagram media item."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.igcomment import IGComment

        reply = IGComment(comment_id, api=api).create_reply(params={"message": message})
        return f"Replied to Instagram comment {comment_id}: new reply {reply.get('id', 'unknown')}"
    except Exception as exc:
        return meta_error(exc)


@tool
def instagram_search_hashtag(
    hashtag: Annotated[str, "Hashtag to search (without the '#')"],
    ig_user_id: Annotated[str, "IG user id (defaults to META_IG_USER_ID)"] = "",
    limit: Annotated[int, "Max recent media to return"] = 10,
) -> str:
    """Search recent media for a hashtag (resolves the tag id, then recent media).

    Note: Meta caps hashtag queries at 30 unique tags per IG user per rolling 7 days.
    """
    uid = ig_user_id.strip() or default_ig_user_id()
    if not uid:
        return _NO_IG_MSG
    tag = hashtag.lstrip("#").strip()
    search, err = graph_get("ig_hashtag_search", {"user_id": uid, "q": tag})
    if err:
        return err
    data = (search or {}).get("data") or []
    if not data:
        return f"No hashtag id found for #{tag}."
    hashtag_id = data[0].get("id")
    media, err = graph_get(
        f"{hashtag_id}/recent_media",
        {
            "user_id": uid,
            "fields": "id,caption,permalink,like_count,comments_count",
            "limit": limit,
        },
    )
    if err:
        return err
    items = (media or {}).get("data") or []
    if not items:
        return f"No recent media found for #{tag}."
    rows = [
        f"  [{it.get('id', '?')}] ♥{it.get('like_count', 0)} 💬{it.get('comments_count', 0)} "
        f"{it.get('permalink', '')}\n    {(it.get('caption') or '')[:120]}"
        for it in items
    ]
    return f"Recent media for #{tag}:\n" + "\n".join(rows)


get_tools, get_tool = make_toolkit_api(globals())
