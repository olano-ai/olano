"""Reddit toolkit via PRAW (Python Reddit API Wrapper).

Auth (checked in this order):
  REDDIT_REFRESH_TOKEN   — OAuth refresh token minted via the dashboard
                           Connections tab (connector broker). Combined with
                           CLIENT_ID + CLIENT_SECRET it yields full user
                           context for reads AND writes; praw auto-refreshes
                           it (Reddit does not rotate refresh tokens, so praw
                           refreshing independently of the broker is safe).
  REDDIT_CLIENT_ID       — App client ID
  REDDIT_CLIENT_SECRET   — App client secret
  REDDIT_USER_AGENT      — Required by Reddit (e.g. 'olano-bot by /u/name')
  REDDIT_USERNAME        — (fallback) for authenticated/write actions
  REDDIT_PASSWORD        — (fallback) for authenticated/write actions

Read-only operations work with just CLIENT_ID + CLIENT_SECRET + USER_AGENT.
Writes (post_submit) additionally need a connected Reddit account (refresh
token) or username/password.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._install_hint import optional_import
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_REDDIT_CREDS_MSG = (
    "[missing-credentials] Reddit credentials missing. Set REDDIT_CLIENT_ID, "
    "REDDIT_CLIENT_SECRET, and REDDIT_USER_AGENT in the vault, or connect "
    "Reddit in the dashboard Connections tab. For write actions also set "
    "REDDIT_USERNAME and REDDIT_PASSWORD (not needed when connected)."
)


def _reddit(write: bool = False) -> tuple[Any, str | None]:
    """Return (Reddit instance, error_string).

    Auth precedence: a broker-minted ``REDDIT_REFRESH_TOKEN`` (dashboard
    Connections tab) yields a full user-context client covering reads AND
    writes; otherwise fall back to username/password (writes) or the app-only
    client-credentials grant (reads).
    """
    praw, err = optional_import("praw", purpose="Reddit API access")
    if err:
        return None, err
    assert praw is not None
    cid = get_credential("REDDIT_CLIENT_ID")
    secret = get_credential("REDDIT_CLIENT_SECRET")
    ua = get_credential("REDDIT_USER_AGENT", "olano-bot/1.0")
    if not cid or not secret:
        return None, _REDDIT_CREDS_MSG
    kwargs: dict = {"client_id": cid, "client_secret": secret, "user_agent": ua}
    refresh_token = get_credential("REDDIT_REFRESH_TOKEN")
    if refresh_token:
        # Connections-tab OAuth: full user context. Reddit does not rotate
        # refresh tokens, so praw refreshing on its own is safe.
        kwargs["refresh_token"] = refresh_token
    elif write:
        u = get_credential("REDDIT_USERNAME")
        p = get_credential("REDDIT_PASSWORD")
        if not u or not p:
            return None, (
                "[missing-credentials] REDDIT_USERNAME and REDDIT_PASSWORD required "
                "for write actions — or connect Reddit in the dashboard Connections tab."
            )
        kwargs["username"] = u
        kwargs["password"] = p
    try:
        return praw.Reddit(**kwargs), None
    except Exception as exc:
        return None, f"[tool-error] Reddit auth: {exc}"


@tool
def reddit_search(
    query: Annotated[str, "Search query"],
    subreddit: Annotated[str, "Limit to a subreddit (empty = all)"] = "",
    sort: Annotated[str, "'relevance', 'hot', 'new', 'top', 'comments'"] = "relevance",
    time_filter: Annotated[str, "'all', 'day', 'week', 'month', 'year'"] = "all",
    limit: Annotated[int, "Max submissions"] = 25,
) -> str:
    """Search Reddit submissions."""
    reddit, err = _reddit()
    if err:
        return err
    try:
        target = reddit.subreddit(subreddit) if subreddit else reddit.subreddit("all")
        results = list(target.search(query, sort=sort, time_filter=time_filter, limit=limit))
        if not results:
            return f"No Reddit results for '{query}'."
        lines = [f"Reddit search '{query}' in r/{subreddit or 'all'} ({len(results)}):"]
        for s in results:
            lines.append(
                f"- [{getattr(s, 'id', '?')}] r/{getattr(s.subreddit, 'display_name', '?')} | "
                f"↑{getattr(s, 'score', 0)} 💬{getattr(s, 'num_comments', 0)}\n"
                f"  {getattr(s, 'title', '?')[:200]}\n"
                f"  https://reddit.com{getattr(s, 'permalink', '')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] reddit_search: {exc}"


@tool
def reddit_get_subreddit_posts(
    subreddit: Annotated[str, "Subreddit name (no r/ prefix)"],
    sort: Annotated[str, "'hot', 'new', 'top', 'rising', 'controversial'"] = "hot",
    time_filter: Annotated[
        str, "Only for 'top'/'controversial': all, day, week, month, year"
    ] = "day",
    limit: Annotated[int, "Max posts"] = 25,
) -> str:
    """Get posts from a subreddit by sort order."""
    reddit, err = _reddit()
    if err:
        return err
    try:
        sub = reddit.subreddit(subreddit)
        if sort == "hot":
            posts = sub.hot(limit=limit)
        elif sort == "new":
            posts = sub.new(limit=limit)
        elif sort == "rising":
            posts = sub.rising(limit=limit)
        elif sort == "controversial":
            posts = sub.controversial(time_filter=time_filter, limit=limit)
        else:
            posts = sub.top(time_filter=time_filter, limit=limit)
        posts = list(posts)
        if not posts:
            return f"No {sort} posts in r/{subreddit}."
        lines = [f"r/{subreddit} {sort} ({len(posts)}):"]
        for p in posts:
            flair = f" [{p.link_flair_text}]" if getattr(p, "link_flair_text", None) else ""
            lines.append(
                f"- [{getattr(p, 'id', '?')}]{flair} ↑{getattr(p, 'score', 0)} "
                f"💬{getattr(p, 'num_comments', 0)} by u/{getattr(p.author, 'name', 'deleted') if p.author else 'deleted'}\n"
                f"  {getattr(p, 'title', '?')[:200]}\n"
                f"  https://reddit.com{getattr(p, 'permalink', '')}"
            )
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] reddit_get_subreddit_posts({subreddit}): {exc}"


@tool
def reddit_get_post_comments(
    post_id: Annotated[str, "Reddit submission ID (e.g. '15ab3cd')"],
    limit: Annotated[int, "Max top-level comments"] = 20,
    sort: Annotated[str, "'best', 'top', 'new', 'controversial', 'old', 'qa'"] = "best",
) -> str:
    """Get top-level comments on a Reddit submission."""
    reddit, err = _reddit()
    if err:
        return err
    try:
        sub = reddit.submission(id=post_id)
        sub.comment_sort = sort
        sub.comments.replace_more(limit=0)
        comments = sub.comments[:limit]
        if not comments:
            return f"No comments on post {post_id}."
        lines = [
            f"r/{sub.subreddit.display_name} | {sub.title[:200]}",
            f"Top {len(comments)} comments (sort={sort}):",
        ]
        for c in comments:
            author = getattr(c.author, "name", "deleted") if c.author else "deleted"
            body = (getattr(c, "body", "") or "")[:300].replace("\n", " ")
            lines.append(f"  [u/{author}] ↑{getattr(c, 'score', 0)} {body}")
        return "\n".join(lines)
    except Exception as exc:
        return f"[tool-error] reddit_get_post_comments({post_id}): {exc}"


@tool
def reddit_post_submit(
    subreddit: Annotated[str, "Subreddit to post to (no r/ prefix)"],
    title: Annotated[str, "Submission title (max 300 chars)"],
    body: Annotated[str, "Self-post body (markdown)"] = "",
    url: Annotated[str, "URL for a link submission (mutually exclusive with body)"] = "",
) -> str:
    """Submit a post to a subreddit. Requires a connected Reddit account
    (Connections tab) or REDDIT_USERNAME + REDDIT_PASSWORD."""
    if body and url:
        return "[tool-error] reddit_post_submit: provide either body OR url, not both."
    reddit, err = _reddit(write=True)
    if err:
        return err
    try:
        sub = reddit.subreddit(subreddit)
        if url:
            s = sub.submit(title=title[:300], url=url)
        else:
            s = sub.submit(title=title[:300], selftext=body)
        return f"Submitted to r/{subreddit}: [{s.id}] {s.title}\n  https://reddit.com{s.permalink}"
    except Exception as exc:
        return f"[tool-error] reddit_post_submit: {exc}"


get_tools, get_tool = make_toolkit_api(globals())
