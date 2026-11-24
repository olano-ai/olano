"""YouTube Data API tools.

Auth: ``YOUTUBE_API_KEY`` (primary, sent as the ``key=`` query param). When
the key is unset, falls back to the shared Google OAuth token — connect
Google in the dashboard Connections tab or run ``olano auth google``
(needs the ``youtube.readonly`` scope) — sent as an ``Authorization:
Bearer`` header on the same REST calls.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_YT_CREDS_MSG = (
    "[missing-credentials] YOUTUBE_API_KEY not set in vault — set it, or "
    "connect Google in the dashboard Connections tab (YouTube falls back to "
    "the Google OAuth token)."
)


def _yt_params(**extra) -> tuple[dict, dict, str | None]:
    """Resolve YouTube auth → ``(params, headers, error)``.

    ``YOUTUBE_API_KEY`` stays primary (``key=`` query param, no headers). When
    it is absent, fall back to the Google OAuth token
    (``google_tools._get_credentials()``, auto-refreshing) sent as an
    ``Authorization: Bearer`` header instead of the ``key`` param.
    """
    key = get_credential("YOUTUBE_API_KEY", "")
    if key:
        return {"key": key, **extra}, {}, None
    try:
        from olano.tools.google_tools import _get_credentials

        creds = _get_credentials()
    except RuntimeError:
        # Not connected to Google either — same remediation guidance.
        return {}, {}, _YT_CREDS_MSG
    except Exception as exc:  # e.g. refresh failure on a stale token
        return {}, {}, f"[tool-error] YouTube auth via Google OAuth failed: {exc}"
    token = getattr(creds, "token", None)
    if not token:
        return {}, {}, _YT_CREDS_MSG
    return dict(extra), {"Authorization": f"Bearer {token}"}, None


@tool
def youtube_list_videos(
    channel_id: Annotated[str, "YouTube channel ID"],
    max_results: Annotated[int, "Max videos to return"] = 10,
) -> str:
    """List videos from a YouTube channel."""
    params, headers, err = _yt_params(
        channelId=channel_id,
        part="snippet",
        order="date",
        maxResults=max_results,
        type="video",
    )
    if err:
        return err
    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        lines = []
        for item in items:
            snip = item.get("snippet", {})
            vid_id = item.get("id", {}).get("videoId", "?")
            lines.append(
                f"- [{vid_id}] {snip.get('title', '?')} ({(snip.get('publishedAt') or '')[:10]})"
            )
        return "\n".join(lines) if lines else "No videos found."
    except Exception as exc:
        return f"[tool-error] youtube_list_videos: {exc}"


@tool
def youtube_get_analytics(
    video_id: Annotated[str, "YouTube video ID"],
) -> str:
    """Get view count, likes, and comments for a YouTube video."""
    params, headers, err = _yt_params(id=video_id, part="statistics,snippet")
    if err:
        return err
    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return f"Video {video_id} not found."
        stats = items[0].get("statistics", {})
        title = items[0].get("snippet", {}).get("title", "?")
        return (
            f"Title: {title}\n"
            f"Views: {stats.get('viewCount', 0)}\n"
            f"Likes: {stats.get('likeCount', 0)}\n"
            f"Comments: {stats.get('commentCount', 0)}"
        )
    except Exception as exc:
        return f"[tool-error] youtube_get_analytics: {exc}"


@tool
def youtube_list_comments(
    video_id: Annotated[str, "YouTube video ID"],
    max_results: Annotated[int, "Max comments"] = 20,
) -> str:
    """List top-level comments on a YouTube video."""
    params, headers, err = _yt_params(
        videoId=video_id, part="snippet", maxResults=max_results, order="relevance"
    )
    if err:
        return err
    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/commentThreads",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        lines = []
        for item in items:
            comment = item.get("snippet", {}).get("topLevelComment", {}).get("snippet", {})
            lines.append(
                f"- [{comment.get('authorDisplayName', '?')}] "
                f"{(comment.get('textDisplay') or '')[:150]}"
            )
        return "\n".join(lines) if lines else "No comments found."
    except Exception as exc:
        return f"[tool-error] youtube_list_comments: {exc}"


@tool
def youtube_search(
    query: Annotated[str, "Search query string"],
    max_results: Annotated[int, "Max results to return"] = 10,
) -> str:
    """Search YouTube videos by keyword."""
    params, headers, err = _yt_params(
        q=query,
        part="snippet",
        type="video",
        maxResults=max_results,
        order="relevance",
    )
    if err:
        return err
    try:
        r = httpx.get(
            "https://www.googleapis.com/youtube/v3/search",
            params=params,
            headers=headers,
            timeout=10,
        )
        r.raise_for_status()
        items = r.json().get("items", [])
        if not items:
            return f"No videos found for '{query}'."
        lines = []
        for item in items:
            snip = item.get("snippet", {})
            vid_id = item.get("id", {}).get("videoId", "?")
            lines.append(f"- [{vid_id}] {snip.get('title', '?')} — {snip.get('channelTitle', '?')}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] youtube_search: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
