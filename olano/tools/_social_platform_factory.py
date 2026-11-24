"""Factory for consistent creator/community-management social tools."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from langchain_core.tools import StructuredTool

from olano.tools._short_video_client import api_request


def make_social_tools(platform: str) -> list[StructuredTool]:
    """Build a broad creator/community-management surface.

    The paths form Olano's social-provider contract.  A platform's approved
    API product or partner gateway may implement only a subset; in that case
    its permission/unsupported response is returned verbatim to the agent.
    """

    def call(method: str, path: str, **kwargs: Any) -> str:
        return api_request(platform, method, path, **kwargs)

    def profile() -> str:
        return call("GET", "me")

    def read_user(user_id: str) -> str:
        return call("GET", f"users/{user_id}")

    def search_posts(query: str, limit: int = 20, cursor: str = "") -> str:
        return call(
            "GET", "posts/search", params={"query": query, "limit": limit, "cursor": cursor}
        )

    def list_posts(limit: int = 20, cursor: str = "") -> str:
        return call("GET", "posts", params={"limit": limit, "cursor": cursor})

    def read_post(post_id: str) -> str:
        return call("GET", f"posts/{post_id}")

    def create_post(
        caption: str,
        media_urls: list[str],
        post_type: str = "video",
        visibility: str = "public",
        publish_at: str = "",
    ) -> str:
        return call(
            "POST",
            "posts",
            payload={
                "caption": caption,
                "media_urls": media_urls,
                "post_type": post_type,
                "visibility": visibility,
                "publish_at": publish_at,
            },
        )

    def update_post(post_id: str, caption: str) -> str:
        return call("PATCH", f"posts/{post_id}", payload={"caption": caption})

    def delete_post(post_id: str) -> str:
        return call("DELETE", f"posts/{post_id}")

    def like_post(post_id: str) -> str:
        return call("PUT", f"posts/{post_id}/like")

    def unlike_post(post_id: str) -> str:
        return call("DELETE", f"posts/{post_id}/like")

    def bookmark_post(post_id: str) -> str:
        return call("PUT", f"posts/{post_id}/bookmark")

    def remove_bookmark(post_id: str) -> str:
        return call("DELETE", f"posts/{post_id}/bookmark")

    def read_comments(post_id: str, limit: int = 50, cursor: str = "") -> str:
        return call("GET", f"posts/{post_id}/comments", params={"limit": limit, "cursor": cursor})

    def reply_to_comment(comment_id: str, message: str) -> str:
        return call("POST", f"comments/{comment_id}/replies", payload={"message": message})

    def read_comment_replies(comment_id: str, limit: int = 50, cursor: str = "") -> str:
        return call(
            "GET", f"comments/{comment_id}/replies", params={"limit": limit, "cursor": cursor}
        )

    def react_to_comment(comment_id: str, reaction: str = "like") -> str:
        return call("PUT", f"comments/{comment_id}/reaction", payload={"reaction": reaction})

    def remove_comment_reaction(comment_id: str) -> str:
        return call("DELETE", f"comments/{comment_id}/reaction")

    def hide_comment(comment_id: str, hidden: bool = True) -> str:
        return call("PATCH", f"comments/{comment_id}", payload={"hidden": hidden})

    def delete_comment(comment_id: str) -> str:
        return call("DELETE", f"comments/{comment_id}")

    def read_dms(limit: int = 50, cursor: str = "") -> str:
        return call("GET", "conversations", params={"limit": limit, "cursor": cursor})

    def reply_to_dm(conversation_id: str, message: str) -> str:
        return call(
            "POST", f"conversations/{conversation_id}/messages", payload={"message": message}
        )

    def read_dm_messages(conversation_id: str, limit: int = 50, cursor: str = "") -> str:
        return call(
            "GET",
            f"conversations/{conversation_id}/messages",
            params={"limit": limit, "cursor": cursor},
        )

    def send_dm(recipient_id: str, message: str) -> str:
        return call(
            "POST", "conversations", payload={"recipient_id": recipient_id, "message": message}
        )

    def react_to_dm(message_id: str, reaction: str) -> str:
        return call("PUT", f"messages/{message_id}/reaction", payload={"reaction": reaction})

    def remove_dm_reaction(message_id: str) -> str:
        return call("DELETE", f"messages/{message_id}/reaction")

    def mark_dm_read(conversation_id: str) -> str:
        return call("POST", f"conversations/{conversation_id}/read")

    def follow_user(user_id: str) -> str:
        return call("PUT", f"users/{user_id}/follow")

    def unfollow_user(user_id: str) -> str:
        return call("DELETE", f"users/{user_id}/follow")

    def post_analytics(post_id: str) -> str:
        return call("GET", f"posts/{post_id}/analytics")

    def account_analytics(start_date: str, end_date: str) -> str:
        return call("GET", "me/analytics", params={"start_date": start_date, "end_date": end_date})

    descriptions = {
        "profile": "Read the authenticated creator/business profile.",
        "read_user": "Read a public creator profile by platform user ID.",
        "search_posts": "Search posts available to the connected API product.",
        "list_posts": "List posts owned by the authenticated account.",
        "read_post": "Read one post and its current public metrics.",
        "create_post": "Create or schedule a video, image, or carousel post from public media URLs.",
        "update_post": "Update the caption of a post owned by the authenticated account.",
        "delete_post": "Permanently delete a post owned by the authenticated account.",
        "like_post": "Like a post as the authenticated account.",
        "unlike_post": "Remove the authenticated account's like from a post.",
        "bookmark_post": "Bookmark or favorite a post for the authenticated account.",
        "remove_bookmark": "Remove a post from the authenticated account's bookmarks.",
        "read_comments": "Read comments on a post, including pagination metadata.",
        "reply_to_comment": "Reply to a comment on the authenticated account's post.",
        "read_comment_replies": "Read threaded replies to a comment.",
        "react_to_comment": "Add a supported reaction (including emoji when supported) to a comment.",
        "remove_comment_reaction": "Remove the authenticated account's reaction from a comment.",
        "hide_comment": "Hide or unhide a comment on a post owned by the authenticated account.",
        "delete_comment": "Delete a comment where the connected API permits moderation.",
        "read_dms": "Read the authenticated business inbox/conversations.",
        "reply_to_dm": "Send a reply in an existing business conversation.",
        "read_dm_messages": "Read messages in one business conversation.",
        "send_dm": "Start a direct-message conversation where the connected API permits it.",
        "react_to_dm": "Add a supported reaction or emoji to a direct message.",
        "remove_dm_reaction": "Remove the authenticated account's reaction from a direct message.",
        "mark_dm_read": "Mark a business conversation as read.",
        "follow_user": "Follow a creator where the connected API permits it.",
        "unfollow_user": "Unfollow a creator where the connected API permits it.",
        "post_analytics": "Read analytics for a post owned by the authenticated account.",
        "account_analytics": "Read account analytics for an inclusive ISO-8601 date range.",
    }
    functions: list[Callable[..., str]] = [
        profile,
        read_user,
        search_posts,
        list_posts,
        read_post,
        create_post,
        update_post,
        delete_post,
        like_post,
        unlike_post,
        bookmark_post,
        remove_bookmark,
        read_comments,
        reply_to_comment,
        read_comment_replies,
        react_to_comment,
        remove_comment_reaction,
        hide_comment,
        delete_comment,
        read_dms,
        reply_to_dm,
        read_dm_messages,
        send_dm,
        react_to_dm,
        remove_dm_reaction,
        mark_dm_read,
        follow_user,
        unfollow_user,
        post_analytics,
        account_analytics,
    ]
    return [
        StructuredTool.from_function(
            fn,
            name=f"{platform}_{fn.__name__}",
            description=descriptions[fn.__name__],
        )
        for fn in functions
    ]
