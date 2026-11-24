"""Buffer social-scheduling tools (GraphQL API).

Buffer is the cross-platform queue: one call schedules the same post to LinkedIn,
X, Facebook, Instagram and the rest, instead of holding a separate integration
and token per network.

Auth env var: ``BUFFER_ACCESS_TOKEN`` (a Buffer API key — Settings → API).
Optional: ``BUFFER_ORGANIZATION_ID`` pins the organization when an account has
more than one; unset, the first organization on the account is used.

**Why GraphQL.** These tools used to call the v1 REST API at
``api.bufferapp.com/1/``. Buffer is retiring it — new client registration is
already closed, with brownouts through late 2026 and full shutdown in 2027 — so
that code had an expiry date. The GraphQL API also carries the thing the REST
tools could never do: ``assets``, i.e. scheduling a post **with an image**.

Every operation is one ``POST https://api.buffer.com`` with a bearer token.
Mutations return a union (``PostActionSuccess`` | ``MutationError``), so a
rejected post arrives as HTTP 200 with a message rather than an exception —
:func:`_unwrap_mutation` is what turns that into a ``[tool-error]`` instead of a
silent success.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

import httpx
from langchain_core.tools import tool

from olano.tools._credentials import get_credential
from olano.tools._toolkit import make_toolkit_api

logger = logging.getLogger(__name__)

_API_URL = "https://api.buffer.com"
_TIMEOUT = 20.0
_BUFFER_CREDS_MSG = "[missing-credentials] BUFFER_ACCESS_TOKEN not set in vault."

# Buffer's SchedulingType / PostMode enums, spelled as the API expects them.
_SCHEDULING_AUTOMATIC = "automatic"
_MODE_QUEUE = "addToQueue"
_MODE_CUSTOM = "customScheduled"
_MODE_DRAFT = "draft"


def _gql(query: str, variables: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
    """Run one GraphQL operation. Returns ``(data, error)`` — exactly one is set.

    GraphQL answers 200 for a query that failed, with the reason under
    ``errors``; treating a 200 as success is how a broken query becomes an empty
    list the caller reports as "no channels connected".
    """
    token = get_credential("BUFFER_ACCESS_TOKEN", "")
    if not token:
        return None, _BUFFER_CREDS_MSG
    try:
        r = httpx.post(
            _API_URL,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"query": query, "variables": variables or {}},
            timeout=_TIMEOUT,
        )
        r.raise_for_status()
        payload = r.json()
    except Exception as exc:
        return None, f"{exc}"

    errors = payload.get("errors")
    if errors:
        messages = "; ".join(str(e.get("message", e)) for e in errors if isinstance(e, dict) or e)
        return None, messages or "Buffer returned an unspecified GraphQL error."
    data = payload.get("data")
    if not isinstance(data, dict):
        return None, "Buffer returned no data."
    return data, ""


def _unwrap_mutation(result: Any, success_key: str) -> tuple[dict[str, Any] | None, str]:
    """Split Buffer's ``PostActionSuccess | MutationError`` union.

    Both arms come back as an ordinary object on a 200 response. Without this
    split a rejected post ("channel not connected", "text too long for X")
    reads as a successful schedule and nobody finds out until the queue is empty.
    """
    if not isinstance(result, dict):
        return None, "Buffer returned an unexpected response shape."
    if result.get("message"):
        return None, str(result["message"])
    payload = result.get(success_key)
    if payload is None:
        return None, f"Buffer returned no {success_key} in its response."
    return payload if isinstance(payload, dict) else {"value": payload}, ""


def _organization_id() -> tuple[str, str]:
    """Resolve the organization to act on. Returns ``(org_id, error)``.

    Almost every Buffer query is scoped to an organization, so this runs first
    on nearly every call. An operator with several organizations pins one with
    ``BUFFER_ORGANIZATION_ID``; everyone else gets the account's first.
    """
    pinned = get_credential("BUFFER_ORGANIZATION_ID", "")
    if pinned:
        return pinned.strip(), ""

    data, err = _gql("query { account { organizations { id name } } }")
    if err:
        return "", err
    orgs = ((data or {}).get("account") or {}).get("organizations") or []
    if not orgs:
        return "", (
            "No Buffer organization found on this account. Set BUFFER_ORGANIZATION_ID "
            "if you know the id, or check that the API key belongs to an active account."
        )
    return str(orgs[0].get("id", "")), ""


def _as_list(value: Any) -> list[str]:
    """Accept a list, a single string, or a comma/newline-separated string.

    Models pass all three shapes for "one or more ids"; rejecting two of them
    turns a working call into a retry loop.
    """
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        parts = [p.strip() for p in value.replace("\n", ",").split(",")]
        return [p for p in parts if p]
    return []


@tool
def buffer_list_channels() -> str:
    """List the social media channels connected to Buffer, with their IDs.

    Call this first: ``buffer_create_post`` needs channel IDs, and they are not
    guessable. The IDs are stable, so one lookup per session is enough.
    """
    org_id, err = _organization_id()
    if err:
        return err if err.startswith("[") else f"[tool-error] buffer_list_channels: {err}"

    data, err = _gql(
        "query ListChannels($input: ChannelsInput!) { channels(input: $input) "
        "{ id name service } }",
        {"input": {"organizationId": org_id}},
    )
    if err:
        return err if err.startswith("[") else f"[tool-error] buffer_list_channels: {err}"

    channels = (data or {}).get("channels") or []
    if not channels:
        return (
            "No channels connected. Connect a social account in Buffer "
            "(https://buffer.com) before scheduling posts."
        )
    return "\n".join(
        f"- [{c.get('id', '?')}] {c.get('service', '?')} / {c.get('name', '')}" for c in channels
    )


@tool
def buffer_create_post(
    text: Annotated[str, "Post text content"],
    channel_ids: Annotated[
        list[str], "Buffer channel IDs to post to — get them from buffer_list_channels"
    ],
    scheduled_at: Annotated[
        str,
        "ISO 8601 UTC datetime to publish at, e.g. '2026-09-01T14:30:00Z'. "
        "Empty = add to the channel's queue at its next slot.",
    ] = "",
    image_urls: Annotated[
        list[str],
        "Public https URLs of images to attach. Buffer fetches these itself, so a local "
        "workspace path will not work — publish the file first (e.g. with pages_start) "
        "or use the platform's own publish tool.",
    ] = [],  # noqa: B006 — langchain reads the default off the signature
    draft: Annotated[
        bool, "Create as a draft for review instead of scheduling it to publish"
    ] = False,
) -> str:
    """Schedule a social media post (optionally with images) through Buffer.

    One call fans out to every channel ID given — the usual way to put the same
    announcement on LinkedIn, X and Facebook at once. Each channel gets its own
    post, so one failing (text too long for X, token expired) does not lose the
    others; the result names which succeeded and which did not.

    ``draft=True`` puts the post in Buffer for a human to approve rather than
    scheduling it, which is the right default when the copy has not been signed
    off yet.
    """
    ids = _as_list(channel_ids)
    if not ids:
        return (
            "[tool-error] buffer_create_post: no channel IDs given. "
            "Call buffer_list_channels first."
        )
    urls = _as_list(image_urls)
    local = [u for u in urls if not u.startswith(("http://", "https://"))]
    if local:
        return (
            f"[tool-error] buffer_create_post: {', '.join(local)} is not a public URL. "
            "Buffer downloads attachments itself and cannot reach the workspace. Publish "
            "the image at a public https URL first, or post it directly with "
            "instagram_publish_image / facebook_publish_post / x_post_tweet."
        )

    if draft:
        mode = _MODE_DRAFT
    elif scheduled_at.strip():
        mode = _MODE_CUSTOM
    else:
        mode = _MODE_QUEUE

    mutation = (
        "mutation CreatePost($input: CreatePostInput!) { createPost(input: $input) "
        "{ ... on PostActionSuccess { post { id text } } "
        "... on MutationError { message } } }"
    )

    scheduled: list[str] = []
    failures: list[str] = []
    for channel_id in ids:
        variables: dict[str, Any] = {
            "input": {
                "text": text,
                "channelId": channel_id,
                "schedulingType": _SCHEDULING_AUTOMATIC,
                "mode": mode,
                "assets": [{"image": {"url": u}} for u in urls],
            }
        }
        if scheduled_at.strip():
            variables["input"]["dueAt"] = scheduled_at.strip()

        data, err = _gql(mutation, variables)
        if err:
            if err.startswith("["):
                return err
            failures.append(f"{channel_id}: {err}")
            continue
        post, err = _unwrap_mutation((data or {}).get("createPost"), "post")
        if err:
            failures.append(f"{channel_id}: {err}")
            continue
        scheduled.append(f"{channel_id} (post {post.get('id', '?')})" if post else channel_id)

    verb = "Drafted" if draft else ("Scheduled" if scheduled_at.strip() else "Queued")
    lines = []
    if scheduled:
        media_note = f" with {len(urls)} image(s)" if urls else ""
        when = f" for {scheduled_at.strip()}" if scheduled_at.strip() else ""
        lines.append(f"{verb} on {len(scheduled)} channel(s){media_note}{when}:")
        lines += [f"  - {s}" for s in scheduled]
    if failures:
        lines.append(f"Failed on {len(failures)} channel(s):")
        lines += [f"  - {f}" for f in failures]
    if not scheduled:
        return "[tool-error] buffer_create_post: " + "\n".join(lines)
    return "\n".join(lines)


@tool
def buffer_list_scheduled(
    limit: Annotated[int, "Maximum posts to return (1-50)"] = 20,
) -> str:
    """List posts waiting in the Buffer queue, with their scheduled times.

    Use this to check what is already booked before scheduling more — a queue
    with three posts booked for the same morning is worth knowing about first.
    """
    org_id, err = _organization_id()
    if err:
        return err if err.startswith("[") else f"[tool-error] buffer_list_scheduled: {err}"

    data, err = _gql(
        "query ScheduledPosts($first: Int!, $input: PostsInput!) { "
        "posts(first: $first, input: $input) { edges { node "
        "{ id text dueAt channelId status } } } }",
        {
            "first": max(1, min(int(limit), 50)),
            "input": {"organizationId": org_id, "filter": {"status": ["scheduled"]}},
        },
    )
    if err:
        return err if err.startswith("[") else f"[tool-error] buffer_list_scheduled: {err}"

    edges = ((data or {}).get("posts") or {}).get("edges") or []
    if not edges:
        return "No scheduled posts."
    lines = []
    for edge in edges:
        node = (edge or {}).get("node") or {}
        text = (node.get("text") or "").replace("\n", " ")[:100]
        lines.append(f"- [{node.get('channelId', '?')}] {text} @ {node.get('dueAt', 'queued')}")
    return "\n".join(lines)


get_tools, get_tool = make_toolkit_api(globals())
