"""Facebook tools (Meta Graph API via the Meta Business SDK).

Agent-facing surface for Facebook Pages: list pages, publish posts, read Page
insights, and moderate comments. Implementation sits on the shared
:mod:`olano.tools._meta_client` layer (SDK + graph escape hatch).

Auth env vars (vault-backed): ``META_ACCESS_TOKEN`` (required),
``META_PAGE_ID`` (optional default Page id).
"""

from __future__ import annotations

import logging
from typing import Annotated

from langchain_core.tools import tool

from olano.tools._meta_client import (
    default_page_id,
    get_api,
    graph_post,
    meta_error,
)
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_NO_PAGE_MSG = (
    "[tool-error] No Facebook Page id provided and META_PAGE_ID is not set. "
    "Pass page_id explicitly or set META_PAGE_ID in the vault."
)


@tool
def facebook_list_pages() -> str:
    """List the Facebook Pages the access token can manage (id, name, category)."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.user import User

        pages = User(fbid="me", api=api).get_accounts(fields=["id", "name", "category"])
        rows = [
            f"  {p.get('name', '?')} (id={p.get('id', '?')}, category={p.get('category', '?')})"
            for p in pages
        ]
        if not rows:
            return "No managed Facebook Pages found for this token."
        return "Facebook Pages:\n" + "\n".join(rows)
    except Exception as exc:
        return meta_error(exc)


@tool
def facebook_publish_post(
    message: Annotated[str, "Text content of the post"],
    page_id: Annotated[str, "Page id to post to (defaults to META_PAGE_ID)"] = "",
    link: Annotated[str, "Optional URL to attach to the post"] = "",
    photo_url: Annotated[str, "Optional public image URL to publish as a photo post"] = "",
) -> str:
    """Publish a post to a Facebook Page (text, optional link, or a photo)."""
    api, err = get_api()
    if err:
        return err
    pid = page_id.strip() or default_page_id()
    if not pid:
        return _NO_PAGE_MSG
    try:
        if photo_url.strip():
            data: dict = {"url": photo_url.strip(), "caption": message}
            result, perr = graph_post(f"{pid}/photos", data)
            if perr or result is None:
                return perr or "[tool-error] facebook_publish_post: no response."
            return f"Facebook photo post published to Page {pid}: {result.get('post_id') or result.get('id')}"

        from facebook_business.adobjects.page import Page

        params: dict = {"message": message}
        if link.strip():
            params["link"] = link.strip()
        post = Page(pid, api=api).create_feed(params=params)
        return f"Facebook post published to Page {pid}: {post.get('id', 'unknown')}"
    except Exception as exc:
        return meta_error(exc)


@tool
def facebook_get_page_insights(
    page_id: Annotated[str, "Page id (defaults to META_PAGE_ID)"] = "",
    metrics: Annotated[
        str, "Comma-separated metrics"
    ] = "page_impressions,page_post_engagements,page_fans",
    period: Annotated[str, "Aggregation period: day, week, or days_28"] = "day",
) -> str:
    """Get insight metrics for a Facebook Page (impressions, engagement, fans)."""
    api, err = get_api()
    if err:
        return err
    pid = page_id.strip() or default_page_id()
    if not pid:
        return _NO_PAGE_MSG
    try:
        from facebook_business.adobjects.page import Page

        results = Page(pid, api=api).get_insights(params={"metric": metrics, "period": period})
        rows: list[str] = []
        for m in results:
            values = m.get("values") or []
            latest = values[-1].get("value") if values else "?"
            rows.append(f"  {m.get('name', '?')}: {latest}")
        if not rows:
            return f"No insight data returned for Page {pid}."
        return f"Facebook Page {pid} insights ({period}):\n" + "\n".join(rows)
    except Exception as exc:
        return meta_error(exc)


@tool
def facebook_get_post_comments(
    post_id: Annotated[str, "Facebook post id (e.g. '<pageid>_<postid>')"],
    limit: Annotated[int, "Max comments to return"] = 25,
) -> str:
    """List comments on a Facebook post (id, author, message, time)."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.pagepost import PagePost

        comments = PagePost(post_id, api=api).get_comments(
            fields=["id", "message", "from", "created_time"],
            params={"limit": limit},
        )
        rows: list[str] = []
        for c in comments:
            author = (c.get("from") or {}).get("name", "?")
            rows.append(
                f"  [{c.get('id', '?')}] {author}: {c.get('message', '')} "
                f"({c.get('created_time', '')})"
            )
        if not rows:
            return f"No comments on post {post_id}."
        return f"Comments on post {post_id}:\n" + "\n".join(rows)
    except Exception as exc:
        return meta_error(exc)


@tool
def facebook_reply_to_comment(
    comment_id: Annotated[str, "Comment id to reply to"],
    message: Annotated[str, "Reply text"],
) -> str:
    """Reply to a comment on a Facebook post."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.comment import Comment

        reply = Comment(comment_id, api=api).create_comment(params={"message": message})
        return f"Replied to comment {comment_id}: new comment {reply.get('id', 'unknown')}"
    except Exception as exc:
        return meta_error(exc)


@tool
def facebook_hide_comment(
    comment_id: Annotated[str, "Comment id to hide or unhide"],
    hide: Annotated[bool, "True to hide, False to unhide"] = True,
) -> str:
    """Hide or unhide a comment on a Facebook post (moderation)."""
    api, err = get_api()
    if err:
        return err
    try:
        from facebook_business.adobjects.comment import Comment

        Comment(comment_id, api=api).api_update(params={"is_hidden": hide})
        verb = "hidden" if hide else "unhidden"
        return f"Comment {comment_id} {verb}."
    except Exception as exc:
        return meta_error(exc)


get_tools, get_tool = make_toolkit_api(globals())
