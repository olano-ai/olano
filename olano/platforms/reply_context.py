"""Quoted-reply context for inbound chat messages.

Every messenger lets a person long-press an earlier message and answer *that
one specifically*. Adapters have always captured it — ``MessageEvent`` carries
``reply_to_message_id`` and ``reply_to_text`` (``platforms/base.py``) and four
adapters populate them — but nothing ever read the text, so the agent saw a
reply as an ordinary message and had no idea what it was about. This module is
the missing half: it renders those fields into a block the runtime prepends to
the message body (``runtime/server.py``).

Shape, mirroring the ``[from …]`` identity tag it sits beside::

    [replying to an earlier message]
    > the quoted text

Five properties are deliberate:

- **The quoted text is sender-selected content, and in a group it is usually
  authored by a third party.** So it goes through the same envelope as the
  message body (``security/untrusted_ingress``), and the ``[replying to …]``
  tag stays *outside* it — runtime-authored and unforgeable, exactly like
  ``[from …]``. Trust is **not** inherited from the current sender, because
  the two are different people: an owner quoting a group attacker would
  otherwise launder that attacker's text into a trusted turn. Since no
  platform hands us the quoted author's identity uniformly, the envelope
  keys on whether the conversation *has* other authors — any group or
  channel, plus any untrusted sender anywhere. A DM with a trusted owner is
  the one case left bare: the only two authors there are that owner and the
  agent itself.
- **Bounded.** A reply to a long agent answer would otherwise re-send that
  whole answer every turn. Quoted text is truncated to
  :data:`MAX_QUOTED_CHARS` on a word boundary with a visible marker.
- **Depth 1.** No walking the reply chain. Telegram's ``reply_to_message``
  does not itself nest, and a chain multiplies tokens for context the thread
  history usually already carries.
- **Never silent.** A reply to a photo with no caption still renders the tag
  (with no quote line), because "they are replying to something" is itself
  the fact the agent was missing.
- **Media is named, and handed over.** A caption-less photo used to render
  only ``(no text — media or unavailable)``, which is true and useless: the
  agent could not tell a photo from a deleted message, and the file itself
  never reached it. Adapters that can retrieve the quoted attachment put it on
  ``reply_to_media_urls``; this module names the kind on the tag line
  (runtime-authored, so outside the envelope) and
  ``inbound.build_agent_message`` saves the file and appends its path. The
  ``unavailable`` wording is kept for the case it was always right about — a
  quote we have neither text nor file for.
"""

from __future__ import annotations

from typing import Any

from olano.security.untrusted_ingress import sender_is_untrusted, wrap_untrusted

#: Quoted text longer than this is truncated. Sized so a quote costs at most a
#: short paragraph: enough to identify any normal message, cheap enough that
#: replying to a long agent answer does not double the turn.
MAX_QUOTED_CHARS = 500

#: Appended when a quote is cut, so the model can tell "short message" from
#: "long message, shown in part" and does not answer as if it saw the whole.
TRUNCATION_MARKER = " … (truncated)"

_TAG = "[replying to an earlier message]"

#: How to name a quoted attachment on the tag line, by mime family. The label
#: is the agent's only cue about *what* was pointed at before it opens the
#: file, so it says the thing a person would say, not the mime.
_MEDIA_LABELS = {
    "image": "a photo",
    "audio": "a voice message",
    "video": "a video",
}
_MEDIA_FALLBACK = "a file"


def quoted_media_label(event: Any) -> str:
    """Name the attachment(s) on the quoted message, or ``""`` when there are none.

    Reads ``reply_to_media_types`` (parallel to ``reply_to_media_urls``), so an
    adapter that retrieved the file but could not type it still gets "a file"
    rather than silence. Several attachments of one kind collapse to a count;
    a mixed set degrades to "files".
    """
    urls = list(getattr(event, "reply_to_media_urls", None) or [])
    if not urls:
        return ""
    types = list(getattr(event, "reply_to_media_types", None) or [])
    families = {
        str(types[i] if i < len(types) else "").split("/", 1)[0].strip().lower()
        for i in range(len(urls))
    }
    known = families & set(_MEDIA_LABELS)
    if len(urls) == 1:
        return _MEDIA_LABELS.get(next(iter(known), ""), _MEDIA_FALLBACK)
    if len(known) == 1 and len(families) == 1:
        # "2 photos" — drop the article the single-item label carries.
        label = _MEDIA_LABELS[next(iter(known))].split(" ", 1)[1]
        return f"{len(urls)} {label}s"
    return f"{len(urls)} files"


def truncate_quote(text: str, limit: int = MAX_QUOTED_CHARS) -> str:
    """Cut *text* to *limit* characters, preferring a word boundary.

    Returns *text* unchanged when it already fits. The marker is appended
    outside the limit — the cap bounds the quoted content, not the notice
    that content was dropped.
    """
    collapsed = (text or "").strip()
    if len(collapsed) <= limit:
        return collapsed
    head = collapsed[:limit]
    # Prefer breaking at whitespace, but only when that keeps most of the
    # budget — a 500-char run with no spaces (a URL, a token, CJK text with
    # no spacing) must not collapse to almost nothing.
    space = head.rfind(" ")
    if space >= limit // 2:
        head = head[:space]
    return head.rstrip() + TRUNCATION_MARKER


def _quote_is_untrusted(event: Any, *, is_trusted: bool, allowlist_configured: bool) -> bool:
    """Whether the quoted text should be enveloped.

    Deliberately *not* the same question as for the message body. The body's
    author is the sender, whose trust the caller already resolved; a quote's
    author is whoever wrote the message being pointed at, and no platform
    reports that identity uniformly. So the test is "could anyone other than
    this sender have written it":

    - **Any group or channel** — a third party plausibly wrote it, whatever
      the sender's own standing. This is the case an owner-trust check would
      miss: an attacker posts an instruction, baits the owner into replying
      to it, and their text arrives inside a trusted turn.
    - **Any untrusted sender**, matching the body's rule, so a stranger's DM
      quote is enveloped too.

    A trusted owner's DM is what remains, and there the only authors are that
    owner and the agent.
    """
    if sender_is_untrusted(is_trusted=is_trusted, allowlist_configured=allowlist_configured):
        return True
    chat_type = str(getattr(getattr(event, "source", None), "chat_type", "") or "").lower()
    return chat_type not in ("", "dm")


def render_reply_context(
    event: Any,
    *,
    is_trusted: bool,
    allowlist_configured: bool,
) -> str:
    """The block naming what an inbound message is replying to.

    Returns ``""`` when the message is not a reply, so the common path stays
    byte-identical to a build without this feature.

    *is_trusted* / *allowlist_configured* carry the same meaning as in
    ``security/untrusted_ingress`` — the caller passes what it already
    computed for the message body.
    """
    reply_id = str(getattr(event, "reply_to_message_id", None) or "").strip()
    quoted = str(getattr(event, "reply_to_text", None) or "").strip()
    media = quoted_media_label(event)
    if not reply_id and not quoted and not media:
        return ""

    # The kind of the quoted attachment rides the tag line: it is the
    # runtime's own statement about the message, never the sender's content,
    # so it belongs outside the envelope exactly like the tag itself.
    tag = f"{_TAG} ({media}, attached below)" if media else _TAG

    if not quoted:
        if media:
            # A caption-less photo. The file is folded into this turn by
            # ``build_agent_message``, so the agent can actually look at it.
            return tag
        # An id with no text and no file: a reply to media we could not
        # retrieve, a sticker, or a platform that gives us only the id. Say so
        # rather than rendering a bare tag that reads as a truncation bug.
        return f"{_TAG} (no text — media or unavailable)"

    body = truncate_quote(quoted)
    quoted_block = "\n".join(f"> {line}" for line in body.split("\n"))
    # Quote-prefix first, envelope around it — so the envelope tags are the
    # runtime's own unmarked lines and the ``>`` markers stay inside them.
    # Safe in this order because ``wrap_untrusted`` neutralizes any tag
    # lookalike in the content before wrapping.
    if _quote_is_untrusted(event, is_trusted=is_trusted, allowlist_configured=allowlist_configured):
        quoted_block = wrap_untrusted(quoted_block)
    return f"{tag}\n{quoted_block}"
