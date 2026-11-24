"""Twitter/X tools (tweepy).

Auth (checked in this order):
    X_USER_ACCESS_TOKEN — OAuth 2.0 user-context bearer minted via the dashboard
        Connections tab (connector broker, auto-refreshed). When present it
        powers EVERY API v2 call here, reads and writes: the client is built as
        ``tweepy.Client(bearer_token=<user token>)`` and user-context methods
        pass ``user_auth=False`` (they default to OAuth 1.0a signing).
    X_API_KEY / X_API_SECRET / X_ACCESS_TOKEN / X_ACCESS_SECRET — legacy
        OAuth 1.0a request signing for the user-context (write) tools.
    X_BEARER_TOKEN — legacy app-only bearer for the read tools.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_X_USER_AUTH_MSG = (
    "[missing-credentials] X user auth not set. Need X_API_KEY, X_API_SECRET, "
    "X_ACCESS_TOKEN, X_ACCESS_SECRET in vault, or connect X in the dashboard "
    "Connections tab."
)
_X_BEARER_MSG = (
    "[missing-credentials] X_BEARER_TOKEN not set in vault — required for read "
    "endpoints — or connect X in the dashboard Connections tab."
)


def _user_client() -> tuple[Any, dict[str, Any], str | None]:
    """Return (client, call_kwargs, error_string) for user-context operations.

    Prefers the broker-minted OAuth 2.0 user token (``X_USER_ACCESS_TOKEN``,
    dashboard Connections tab): tweepy sends it as ``Authorization: Bearer``,
    but its user-context methods default to ``user_auth=True`` (OAuth 1.0a
    signing), so on this path ``call_kwargs`` carries ``{"user_auth": False}``
    for call sites to splat into each request. On the legacy OAuth 1.0a
    four-key fallback ``call_kwargs`` is empty, keeping those calls
    byte-identical to before.
    """
    tweepy, err = optional_import("tweepy", purpose="Twitter/X API access")
    if err:
        return None, {}, err
    assert tweepy is not None  # narrowed: err is None ⇒ import succeeded
    user_token = get_credential("X_USER_ACCESS_TOKEN")
    if user_token:
        return tweepy.Client(bearer_token=user_token), {"user_auth": False}, None
    keys = (
        get_credential("X_API_KEY"),
        get_credential("X_API_SECRET"),
        get_credential("X_ACCESS_TOKEN"),
        get_credential("X_ACCESS_SECRET"),
    )
    if not all(keys):
        return None, {}, _X_USER_AUTH_MSG
    return (
        tweepy.Client(
            consumer_key=keys[0],
            consumer_secret=keys[1],
            access_token=keys[2],
            access_token_secret=keys[3],
        ),
        {},
        None,
    )


def _bearer_client() -> tuple[Any, str | None]:
    """Return (client, error_string) for the read tools.

    The OAuth 2.0 user token (Connections tab) satisfies the read endpoints
    too, so it is preferred; the legacy app-only ``X_BEARER_TOKEN`` remains
    the fallback. Both are sent as ``Authorization: Bearer`` and the read
    methods already default to ``user_auth=False``.
    """
    tweepy, err = optional_import("tweepy", purpose="Twitter/X API access")
    if err:
        return None, err
    bearer = get_credential("X_USER_ACCESS_TOKEN") or get_credential("X_BEARER_TOKEN")
    if not bearer:
        return None, _X_BEARER_MSG
    assert tweepy is not None  # narrowed: err is None ⇒ import succeeded
    return tweepy.Client(bearer_token=bearer), None


@tool
def x_post_tweet(
    text: Annotated[str, "Tweet text (max 280 chars)"],
    reply_to_id: Annotated[str, "Tweet ID to reply to (optional)"] = "",
) -> str:
    """Post a tweet to Twitter/X."""
    client, ua, err = _user_client()
    if err:
        return err
    try:
        kwargs: dict = {"text": text}
        if reply_to_id:
            kwargs["in_reply_to_tweet_id"] = reply_to_id
        resp = client.create_tweet(**kwargs, **ua)
        data = getattr(resp, "data", None) or {}
        tweet_id = data.get("id") if isinstance(data, dict) else getattr(data, "id", "?")
        return f"Tweet posted: https://twitter.com/i/web/status/{tweet_id}"
    except Exception as exc:
        return f"[tool-error] x_post_tweet: {exc}"


@tool
def x_search(
    query: Annotated[str, "Search query"],
    max_results: Annotated[int, "Max results (10-100)"] = 20,
) -> str:
    """Search recent tweets on Twitter/X."""
    client, err = _bearer_client()
    if err:
        return err
    try:
        resp = client.search_recent_tweets(
            query=query,
            max_results=max_results,
            tweet_fields=["created_at", "author_id", "public_metrics"],
        )
        data = getattr(resp, "data", None)
        if not data:
            return "No results found."
        lines = []
        for tweet in data:
            metrics = getattr(tweet, "public_metrics", None) or {}
            created = getattr(tweet, "created_at", "?")
            tid = getattr(tweet, "id", "?")
            text = (getattr(tweet, "text", "") or "")[:200]
            lines.append(
                f"[{created}] id={tid}\n  {text}\n"
                f"  ❤️ {metrics.get('like_count', 0)} 🔁 {metrics.get('retweet_count', 0)}"
            )
        return "\n\n".join(lines)
    except Exception as exc:
        return f"[tool-error] x_search: {exc}"


@tool
def x_timeline(
    user_id: Annotated[str, "User ID (or 'me' for authenticated user)"] = "me",
    max_results: Annotated[int, "Max tweets to return"] = 10,
) -> str:
    """Get timeline tweets for a user."""
    client, ua, err = _user_client()
    if err:
        return err
    try:
        if user_id == "me":
            me = client.get_me(**ua)
            me_data = getattr(me, "data", None)
            user_id = getattr(me_data, "id", "") if me_data else ""
            if not user_id:
                return "[tool-error] x_timeline: could not resolve authenticated user."
        resp = client.get_users_tweets(user_id, max_results=max_results, **ua)
        data = getattr(resp, "data", None)
        if not data:
            return "No tweets found."
        return "\n".join(
            f"- [{getattr(t, 'id', '?')}] {(getattr(t, 'text', '') or '')[:150]}" for t in data
        )
    except Exception as exc:
        return f"[tool-error] x_timeline: {exc}"


@tool
def x_get_user(
    username: Annotated[str, "Twitter handle without @"],
) -> str:
    """Get a Twitter/X user profile by username."""
    client, err = _bearer_client()
    if err:
        return err
    try:
        resp = client.get_user(
            username=username.lstrip("@"),
            user_fields=["description", "public_metrics", "verified", "created_at"],
        )
        data = getattr(resp, "data", None)
        if not data:
            return f"No user found: @{username}"
        metrics = getattr(data, "public_metrics", None) or {}
        return (
            f"@{getattr(data, 'username', '?')} (id={getattr(data, 'id', '?')})\n"
            f"  Name: {getattr(data, 'name', '?')}\n"
            f"  Bio: {getattr(data, 'description', '') or ''}\n"
            f"  Followers: {metrics.get('followers_count', 0)} | "
            f"Following: {metrics.get('following_count', 0)} | "
            f"Tweets: {metrics.get('tweet_count', 0)}\n"
            f"  Verified: {getattr(data, 'verified', '?')} | "
            f"Created: {getattr(data, 'created_at', '?')}"
        )
    except Exception as exc:
        return f"[tool-error] x_get_user: {exc}"


@tool
def x_like_tweet(tweet_id: Annotated[str, "Tweet ID to like"]) -> str:
    """Like a tweet (requires user auth)."""
    client, ua, err = _user_client()
    if err:
        return err
    try:
        client.like(tweet_id=tweet_id, **ua)
        return f"Liked tweet {tweet_id}"
    except Exception as exc:
        return f"[tool-error] x_like_tweet: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
